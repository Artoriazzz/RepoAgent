package com.artoria.repoagentbackend.memory;

import com.artoria.repoagentbackend.common.BadRequestException;
import com.artoria.repoagentbackend.common.ConflictException;
import com.artoria.repoagentbackend.common.NotFoundException;
import com.artoria.repoagentbackend.repair.RepairTask;
import com.artoria.repoagentbackend.repair.RepairTaskJpaRepository;
import com.artoria.repoagentbackend.repository.RepositoryProvider;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;

@Service
public class MemoryService {
    private static final String HISTORICAL_SWE_BENCH_SCOPE = "HISTORICAL_SWE_BENCH";

    private final MemoryObservationJpaRepository observations;
    private final MemoryProposalJpaRepository proposals;
    private final SemanticMemoryJpaRepository memories;
    private final MemoryRevisionJpaRepository revisions;
    private final MemoryIndexJobJpaRepository indexJobs;
    private final MemoryRecallJpaRepository recalls;
    private final MemoryRecallRecorder recallRecorder;
    private final RepairTaskJpaRepository tasks;
    private final MemoryEmbeddingClient embeddings;
    private final MemoryRagCache ragCache;
    private final MemoryQdrantStore qdrant;
    private final ObjectMapper objectMapper;
    private final int maxRecallCandidates;

    public MemoryService(
            MemoryObservationJpaRepository observations,
            MemoryProposalJpaRepository proposals,
            SemanticMemoryJpaRepository memories,
            MemoryRevisionJpaRepository revisions,
            MemoryIndexJobJpaRepository indexJobs,
            MemoryRecallJpaRepository recalls,
            MemoryRecallRecorder recallRecorder,
            RepairTaskJpaRepository tasks,
            MemoryEmbeddingClient embeddings,
            MemoryRagCache ragCache,
            MemoryQdrantStore qdrant,
            ObjectMapper objectMapper,
            @Value("${app.memory.retrieval.max-candidates:200}") int maxRecallCandidates
    ) {
        this.observations = observations;
        this.proposals = proposals;
        this.memories = memories;
        this.revisions = revisions;
        this.indexJobs = indexJobs;
        this.recalls = recalls;
        this.recallRecorder = recallRecorder;
        this.tasks = tasks;
        this.embeddings = embeddings;
        this.ragCache = ragCache;
        this.qdrant = qdrant;
        this.objectMapper = objectMapper;
        this.maxRecallCandidates = Math.max(1, maxRecallCandidates);
    }

    @Transactional
    public MemoryObservationResponse recordObservation(CreateMemoryObservationRequest request) {
        MemoryObservation existing = observations.findByIdempotencyKey(request.idempotencyKey()).orElse(null);
        if (existing != null) {
            if (!existing.getTaskId().equals(request.taskId()) || existing.getOutcome() != request.outcome()) {
                throw new ConflictException("Memory observation idempotency key was used by another result");
            }
            return MemoryObservationResponse.from(existing);
        }
        RepairTask task = getTask(request.taskId());
        UUID repositoryId = task.getIssue().getRepository().getId();
        MemoryObservation observation = new MemoryObservation(
                task.getId(), repositoryId, request.idempotencyKey(), request.outcome(),
                request.problemSignature(), request.patchSummary(), request.validationSummary(),
                writeJson(request.evidence())
        );
        return MemoryObservationResponse.from(observations.save(observation));
    }

    @Transactional
    public MemoryProposalResponse submitProposal(CreateMemoryProposalRequest request) {
        MemoryProposal existing = proposals.findByIdempotencyKey(request.idempotencyKey()).orElse(null);
        if (existing != null) {
            SemanticMemory memory = existing.getMemoryId() == null
                    ? null : memories.findById(existing.getMemoryId()).orElse(null);
            return MemoryProposalResponse.from(existing, memory);
        }
        MemoryObservation observation = observations.findById(request.observationId())
                .orElseThrow(() -> new NotFoundException("Memory observation not found: " + request.observationId()));
        if (observation.getOutcome() != MemoryOutcome.RESOLVED) {
            throw new BadRequestException("Only a RESOLVED observation can update semantic memory");
        }
        validateProposalShape(request);
        String payload = request.content() == null ? null : writeJson(request.content());
        MemoryProposal proposal = proposals.save(new MemoryProposal(
                request.memoryId(), observation.getId(), request.idempotencyKey(), request.operation(),
                request.baseVersion(), request.scopeType(), request.changeReason(), payload
        ));
        return switch (request.operation()) {
            case CREATE -> queueCreateProposal(request, observation, proposal);
            case UPDATE -> queueUpdateProposal(request, observation, proposal);
            case NO_CHANGE -> recordNoChange(request, observation, proposal);
        };
    }

    public MemorySearchResponse search(MemorySearchRequest request) {
        return search(request, safeEmbed(request.embeddingText()));
    }

    /**
     * 同一轮工作流已经拥有问题向量时可直接复用，避免重复请求 Embedding 服务。
     * 该入口也用于受保护的检索基准测试，计时范围不会混入模型推理时间。
     */
    MemorySearchResponse searchWithEmbedding(MemorySearchRequest request, MemoryEmbeddingResult queryEmbedding) {
        return search(request, Optional.ofNullable(queryEmbedding));
    }

    private MemorySearchResponse search(
            MemorySearchRequest request, Optional<MemoryEmbeddingResult> queryEmbedding
    ) {
        RepairTask task = getTask(request.taskId());
        UUID repositoryId = task.getIssue().getRepository().getId();
        List<String> scopeKeys = new ArrayList<>(List.of(globalScope(), repositoryScope(repositoryId)));
        // 这些经验来自已解决的 benchmark 实例，正式 SWE-bench 任务绝不能召回它们。
        if (task.getIssue().getRepository().getProvider() != RepositoryProvider.SWE_BENCH) {
            scopeKeys.add(HISTORICAL_SWE_BENCH_SCOPE);
        }
        Set<UUID> excluded = new HashSet<>(revisions.findMemoryIdsSourcedFromIssue(task.getIssue().getId()));

        if (queryEmbedding.isPresent()) {
            VectorSearchResult vectorResult = vectorSearch(request, scopeKeys, excluded, queryEmbedding.get());
            if (qdrant.isEnabled()) {
                // Qdrant 的成功空结果与服务故障都不触发 MySQL 候选池扫描。经验记忆是辅助
                // 能力，Agent 应在没有记忆的情况下继续，不能把检索故障放大成数据库压力。
                recordRecall(task, request, vectorResult.hits());
                return new MemorySearchResponse(
                        queryEmbedding.get().model(), true, vectorResult.status(), vectorResult.hits()
                );
            }
            if (!vectorResult.hits().isEmpty()) {
                recordRecall(task, request, vectorResult.hits());
                return new MemorySearchResponse(
                        queryEmbedding.get().model(), true, vectorResult.status(), vectorResult.hits()
                );
            }
        }

        List<MemorySearchHit> lexicalHits = lexicalSearch(request, scopeKeys, excluded);
        recordRecall(task, request, lexicalHits);
        return new MemorySearchResponse(
                queryEmbedding.map(MemoryEmbeddingResult::model).orElse(null), false,
                lexicalHits.isEmpty() ? MemoryRetrievalStatus.KEYWORD_EMPTY : MemoryRetrievalStatus.KEYWORD_HIT,
                lexicalHits
        );
    }

    @Transactional
    public MemoryResponse recordUseFeedback(UUID memoryId, MemoryUseFeedbackRequest request) {
        getTask(request.taskId());
        MemoryRecall recall = recalls.findForFeedback(request.taskId(), memoryId, request.revision())
                .orElseThrow(() -> new NotFoundException("Memory recall record not found"));
        SemanticMemory memory = memories.findByIdForUpdate(memoryId)
                .orElseThrow(() -> new NotFoundException("Semantic memory not found: " + memoryId));
        if (recall.recordFeedback(request.used(), request.successful()) && request.used()) {
            memory.recordOutcome(request.successful());
            ragCache.invalidateScopeAfterCommit(memory.getScopeKey());
        }
        return MemoryResponse.from(memory);
    }

    /** CREATE 也先进入待处理池，避免多个成功任务同时制造重复的通用经验。 */
    private MemoryProposalResponse queueCreateProposal(
            CreateMemoryProposalRequest request,
            MemoryObservation observation,
            MemoryProposal proposal
    ) {
        MemoryScopeType scope = request.scopeType() == null ? MemoryScopeType.REPOSITORY : request.scopeType();
        UUID repositoryId = scope == MemoryScopeType.REPOSITORY ? observation.getRepositoryId() : null;
        String scopeKey = scope == MemoryScopeType.GLOBAL ? globalScope() : repositoryScope(repositoryId);
        String fingerprint = MemoryFingerprint.from(request.content());
        SemanticMemory duplicate = memories.findByScopeKeyAndFingerprint(scopeKey, fingerprint).orElse(null);
        if (duplicate != null) {
            proposal.noChange(duplicate.getId(), duplicate.getVersion());
            return MemoryProposalResponse.from(proposal, duplicate);
        }
        return MemoryProposalResponse.from(proposal, null);
    }

    /**
     * UPDATE 的内容只作为提案保存。baseVersion 与当前版本一致时才能加入后续批次；
     * 版本过期的提案保留审计记录，但绝不能自动重放到新版本。
     */
    private MemoryProposalResponse queueUpdateProposal(
            CreateMemoryProposalRequest request,
            MemoryObservation observation,
            MemoryProposal proposal
    ) {
        SemanticMemory memory = memories.findByIdForUpdate(request.memoryId())
                .orElseThrow(() -> new NotFoundException("Semantic memory not found: " + request.memoryId()));
        requireObservationCanModify(observation, memory);
        if (memory.getVersion() != request.baseVersion()) {
            proposal.stale(memory.getId(), memory.getVersion(),
                    "Memory changed after recall; submit a newly reasoned proposal based on the current revision");
            return MemoryProposalResponse.from(proposal, memory);
        }
        return MemoryProposalResponse.from(proposal, memory);
    }

    private MemoryProposalResponse recordNoChange(
            CreateMemoryProposalRequest request,
            MemoryObservation observation,
            MemoryProposal proposal
    ) {
        SemanticMemory memory = memories.findByIdForUpdate(request.memoryId())
                .orElseThrow(() -> new NotFoundException("Semantic memory not found: " + request.memoryId()));
        requireObservationCanModify(observation, memory);
        if (request.baseVersion() != null && memory.getVersion() != request.baseVersion()) {
            proposal.stale(memory.getId(), memory.getVersion(),
                    "Memory changed after recall; the no-change conclusion is no longer based on the current revision");
        } else {
            memory.recordOutcome(true);
            ragCache.invalidateScopeAfterCommit(memory.getScopeKey());
            proposal.noChange(memory.getId(), memory.getVersion());
        }
        return MemoryProposalResponse.from(proposal, memory);
    }

    private void validateProposalShape(CreateMemoryProposalRequest request) {
        if (request.operation() == MemoryProposalOperation.CREATE) {
            if (request.memoryId() != null || request.baseVersion() != null || request.content() == null) {
                throw new BadRequestException("CREATE requires content and forbids memoryId/baseVersion");
            }
            return;
        }
        if (request.memoryId() == null) {
            throw new BadRequestException(request.operation() + " requires memoryId");
        }
        if (request.operation() == MemoryProposalOperation.UPDATE
                && (request.baseVersion() == null || request.content() == null)) {
            throw new BadRequestException("UPDATE requires baseVersion and content");
        }
    }

    private void requireObservationCanModify(MemoryObservation observation, SemanticMemory memory) {
        if (memory.getScopeType() == MemoryScopeType.REPOSITORY
                && !observation.getRepositoryId().equals(memory.getRepositoryId())) {
            throw new BadRequestException("Observation belongs to another repository memory scope");
        }
    }

    private RepairTask getTask(UUID taskId) {
        return tasks.findWithIssueById(taskId)
                .orElseThrow(() -> new NotFoundException("Repair task not found: " + taskId));
    }

    private void enqueueIndex(SemanticMemory memory) {
        if (!embeddings.isEnabled()) return;
        if (indexJobs.findByMemoryIdAndMemoryRevisionAndEmbeddingProfile(
                memory.getId(), memory.getVersion(), embeddings.profile()
        ).isEmpty()) {
            indexJobs.save(new MemoryIndexJob(memory.getId(), memory.getVersion(), embeddings.profile()));
        }
    }

    private Optional<MemoryEmbeddingResult> safeEmbed(String text) {
        try {
            return embeddings.embed(text);
        } catch (RuntimeException ignored) {
            return Optional.empty();
        }
    }

    private VectorSearchResult vectorSearch(
            MemorySearchRequest request,
            List<String> scopeKeys,
            Set<UUID> excluded,
            MemoryEmbeddingResult queryEmbedding
    ) {
        if (qdrant.isEnabled()) {
            return qdrantVectorSearch(request, scopeKeys, excluded, queryEmbedding);
        }

        Map<UUID, MemoryScopeEntry> candidates = new LinkedHashMap<>();
        for (String scopeKey : scopeKeys) {
            for (MemoryScopeEntry entry : scopeEntries(scopeKey)) {
                if (entry.status() == MemoryStatus.ACTIVE && !excluded.contains(entry.memoryId())) {
                    candidates.putIfAbsent(entry.memoryId(), entry);
                }
            }
        }
        if (candidates.isEmpty()) return new VectorSearchResult(MemoryRetrievalStatus.REDIS_VECTOR_EMPTY, List.of());

        Map<UUID, MemoryVectorValue> vectors = vectorsFor(candidates, queryEmbedding);
        List<ScoredMemory> scored = new ArrayList<>();
        for (MemoryScopeEntry entry : candidates.values()) {
            MemoryVectorValue vector = vectors.get(entry.memoryId());
            if (vector == null || !vector.isCompatibleWith(queryEmbedding.model(), queryEmbedding.vector().size())) {
                continue;
            }
            double similarity = MemoryScorer.cosine(queryEmbedding.vector(), vector.vector());
            double score = similarity * 0.9 + entry.qualityScore() * 0.1;
            if (score >= 0.08) {
                scored.add(new ScoredMemory(entry, vector, similarity, score));
            }
        }
        scored.sort(Comparator.comparingDouble(ScoredMemory::score).reversed());
        if (scored.isEmpty()) return new VectorSearchResult(MemoryRetrievalStatus.REDIS_VECTOR_EMPTY, List.of());

        int contentCandidateLimit = Math.min(scored.size(), Math.max(request.effectiveLimit() * 3, request.effectiveLimit()));
        Map<UUID, MemoryContent> contents = contentsFor(
                scored.subList(0, contentCandidateLimit).stream().map(ScoredMemory::entry).toList()
        );
        List<MemorySearchHit> hits = new ArrayList<>();
        for (ScoredMemory candidate : scored) {
            MemoryContent content = contents.get(candidate.entry().memoryId());
            if (content == null) continue;
            hits.add(new MemorySearchHit(
                    MemoryResponse.fromCached(candidate.entry(), content, candidate.vector(), ragCache.embeddingProfile()),
                    round(candidate.score()), 0, round(candidate.similarity()), "embedding+quality"
            ));
            if (hits.size() == request.effectiveLimit()) break;
        }
        return new VectorSearchResult(
                hits.isEmpty() ? MemoryRetrievalStatus.REDIS_VECTOR_EMPTY : MemoryRetrievalStatus.REDIS_VECTOR_HIT,
                List.copyOf(hits)
        );
    }

    /**
     * Qdrant 已携带正文，因此这里只回源校验少量命中的版本与权限作用域，而不再读取、
     * 反序列化和计算整个候选池的向量。
     */
    private VectorSearchResult qdrantVectorSearch(
            MemorySearchRequest request,
            List<String> scopeKeys,
            Set<UUID> excluded,
            MemoryEmbeddingResult queryEmbedding
    ) {
        if (!qdrant.isEnabled()) return new VectorSearchResult(MemoryRetrievalStatus.REDIS_VECTOR_EMPTY, List.of());
        MemoryQdrantQueryResult queryResult = qdrant.search(
                queryEmbedding, scopeKeys, request.effectiveLimit()
        );
        if (queryResult.status() == MemoryQdrantQueryStatus.UNAVAILABLE) {
            return new VectorSearchResult(MemoryRetrievalStatus.MEMORY_UNAVAILABLE, List.of());
        }
        List<MemoryQdrantSearchHit> candidates = queryResult.hits();
        if (candidates.isEmpty()) return new VectorSearchResult(MemoryRetrievalStatus.QDRANT_EMPTY, List.of());

        Set<UUID> ids = candidates.stream().map(hit -> hit.memory().id()).collect(java.util.stream.Collectors.toSet());
        Map<UUID, SemanticMemory> current = new HashMap<>();
        for (SemanticMemory memory : memories.findAllById(ids)) current.put(memory.getId(), memory);

        List<MemorySearchHit> hits = new ArrayList<>();
        Set<UUID> seen = new HashSet<>();
        for (MemoryQdrantSearchHit candidate : candidates) {
            MemoryResponse payload = candidate.memory();
            SemanticMemory memory = current.get(payload.id());
            if (excluded.contains(payload.id()) || memory == null
                    || memory.getStatus() != MemoryStatus.ACTIVE
                    || memory.getVersion() != payload.revision()
                    || !scopeKeys.contains(memory.getScopeKey())
                    || !queryEmbedding.model().equals(payload.embeddingModel())
                    || !seen.add(payload.id())) {
                continue;
            }
            hits.add(new MemorySearchHit(
                    payload, candidate.score(), 0, candidate.similarity(), "qdrant-embedding+quality"
            ));
            if (hits.size() == request.effectiveLimit()) break;
        }
        return new VectorSearchResult(
                hits.isEmpty() ? MemoryRetrievalStatus.QDRANT_EMPTY : MemoryRetrievalStatus.QDRANT_HIT,
                List.copyOf(hits)
        );
    }

    private List<MemorySearchHit> lexicalSearch(
            MemorySearchRequest request, List<String> scopeKeys, Set<UUID> excluded
    ) {
        List<SemanticMemory> candidates = memories.findRecallCandidates(
                scopeKeys, MemoryStatus.ACTIVE, PageRequest.of(0, maxRecallCandidates)
        );
        List<MemorySearchHit> hits = new ArrayList<>();
        for (SemanticMemory memory : candidates) {
            if (excluded.contains(memory.getId())) continue;
            double lexical = MemoryScorer.lexical(request.embeddingText(), memory, request);
            double score = lexical * 0.8 + memory.getQualityScore() * 0.2;
            if (score < 0.08) continue;
            hits.add(new MemorySearchHit(
                    MemoryResponse.from(memory), round(score), round(lexical), null,
                    "keyword+metadata+quality"
            ));
        }
        hits.sort(Comparator.comparingDouble(MemorySearchHit::score).reversed());
        return hits.stream().limit(request.effectiveLimit()).toList();
    }

    private List<MemoryScopeEntry> scopeEntries(String scopeKey) {
        return ragCache.findOrLoadScopeEntries(scopeKey, () -> {
            List<MemoryScopeEntry> entries = memories.findRecallCandidates(
                    List.of(scopeKey), MemoryStatus.ACTIVE, PageRequest.of(0, maxRecallCandidates)
            ).stream().map(MemoryScopeEntry::from).toList();
            return entries;
        });
    }

    private Map<UUID, MemoryVectorValue> vectorsFor(
            Map<UUID, MemoryScopeEntry> entries, MemoryEmbeddingResult queryEmbedding
    ) {
        Map<UUID, MemoryVectorValue> result = new HashMap<>();
        Map<UUID, MemoryScopeEntry> misses = new HashMap<>();
        Map<UUID, MemoryVectorValue> cached = ragCache.findVectors(List.copyOf(entries.values()));
        for (MemoryScopeEntry entry : entries.values()) {
            MemoryVectorValue vector = cached.get(entry.memoryId());
            if (vector != null && vector.isCompatibleWith(queryEmbedding.model(), queryEmbedding.vector().size())) {
                result.put(entry.memoryId(), vector);
            } else {
                misses.put(entry.memoryId(), entry);
            }
        }
        if (misses.isEmpty()) return result;
        for (SemanticMemory memory : memories.findAllById(misses.keySet())) {
            MemoryScopeEntry entry = misses.get(memory.getId());
            if (entry == null || memory.getVersion() != entry.revision()
                    || memory.getStatus() != MemoryStatus.ACTIVE) continue;
            storedVector(memory, queryEmbedding).ifPresent(vector -> {
                ragCache.cacheVector(memory.getId(), memory.getVersion(), vector);
                result.put(memory.getId(), vector);
            });
        }
        return result;
    }

    private Map<UUID, MemoryContent> contentsFor(List<MemoryScopeEntry> entries) {
        Map<UUID, MemoryContent> result = new HashMap<>(ragCache.findContents(entries));
        Map<UUID, MemoryScopeEntry> misses = new HashMap<>();
        for (MemoryScopeEntry entry : entries) {
            if (!result.containsKey(entry.memoryId())) misses.put(entry.memoryId(), entry);
        }
        if (misses.isEmpty()) return result;
        for (SemanticMemory memory : memories.findAllById(misses.keySet())) {
            MemoryScopeEntry entry = misses.get(memory.getId());
            if (entry == null || memory.getVersion() != entry.revision()
                    || memory.getStatus() != MemoryStatus.ACTIVE) continue;
            MemoryContent content = memory.content();
            ragCache.cacheContent(memory.getId(), memory.getVersion(), content);
            result.put(memory.getId(), content);
        }
        return result;
    }

    private Optional<MemoryVectorValue> storedVector(SemanticMemory memory, MemoryEmbeddingResult queryEmbedding) {
        if (memory.getEmbeddingJson() == null || memory.getEmbeddingRevision() == null
                || memory.getEmbeddingRevision() != memory.getVersion()
                || !queryEmbedding.model().equals(memory.getEmbeddingModel())
                || !ragCache.embeddingProfile().equals(memory.getEmbeddingProfile())) {
            return Optional.empty();
        }
        try {
            List<Double> vector = objectMapper.readValue(
                    memory.getEmbeddingJson(), new TypeReference<List<Double>>() { }
            );
            if (vector.size() != queryEmbedding.vector().size() || vector.stream().anyMatch(value ->
                    value == null || !Double.isFinite(value))) {
                return Optional.empty();
            }
            return Optional.of(new MemoryVectorValue(
                    memory.getEmbeddingModel(), vector.size(), vector
            ));
        } catch (JsonProcessingException ignored) {
            return Optional.empty();
        }
    }

    private void recordRecall(RepairTask task, MemorySearchRequest request, List<MemorySearchHit> hits) {
        if (request.shouldTrackRecall()) recallRecorder.record(task.getId(), hits);
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (JsonProcessingException error) {
            throw new BadRequestException("Memory payload is not valid JSON");
        }
    }

    private static String globalScope() {
        return "GLOBAL";
    }

    private static String repositoryScope(UUID repositoryId) {
        return "REPOSITORY:" + repositoryId;
    }

    private static double round(double value) {
        return Math.round(value * 10_000d) / 10_000d;
    }

    private record ScoredMemory(
            MemoryScopeEntry entry, MemoryVectorValue vector, double similarity, double score
    ) {
    }

    private record VectorSearchResult(MemoryRetrievalStatus status, List<MemorySearchHit> hits) {
    }
}
