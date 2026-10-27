package com.artoria.repoagentbackend.memory;

import com.artoria.repoagentbackend.common.BadRequestException;
import com.artoria.repoagentbackend.common.ConflictException;
import com.artoria.repoagentbackend.common.NotFoundException;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

/**
 * 将多个成功修复形成的提案按同一正式版本合并。这里不调用 LLM：它只负责领取固定输入、
 * 保存租约和原子发布；实际 Memory Agent 在事务外读取 work，再调用 complete 提交结果。
 */
@Service
public class MemoryMergeBatchService {
    private final MemoryProposalJpaRepository proposals;
    private final MemoryMergeBatchJpaRepository batches;
    private final SemanticMemoryJpaRepository memories;
    private final MemoryObservationJpaRepository observations;
    private final MemoryRevisionJpaRepository revisions;
    private final MemoryIndexJobJpaRepository indexJobs;
    private final MemoryEmbeddingClient embeddings;
    private final MemoryRagCache ragCache;
    private final ObjectMapper objectMapper;
    private final int maxProposalsPerBatch;
    private final int claimScanSize;
    private final int recoveryBatchSize;
    private final Duration proposalHoldDuration;

    public MemoryMergeBatchService(
            MemoryProposalJpaRepository proposals,
            MemoryMergeBatchJpaRepository batches,
            SemanticMemoryJpaRepository memories,
            MemoryObservationJpaRepository observations,
            MemoryRevisionJpaRepository revisions,
            MemoryIndexJobJpaRepository indexJobs,
            MemoryEmbeddingClient embeddings,
            MemoryRagCache ragCache,
            ObjectMapper objectMapper,
            @Value("${app.memory.merge.max-proposals-per-batch:12}") int maxProposalsPerBatch,
            @Value("${app.memory.merge.claim-scan-size:24}") int claimScanSize,
            @Value("${app.memory.merge.recovery-batch-size:50}") int recoveryBatchSize,
            @Value("${app.memory.merge.proposal-hold-seconds:60}") long proposalHoldSeconds
    ) {
        this.proposals = proposals;
        this.batches = batches;
        this.memories = memories;
        this.observations = observations;
        this.revisions = revisions;
        this.indexJobs = indexJobs;
        this.embeddings = embeddings;
        this.ragCache = ragCache;
        this.objectMapper = objectMapper;
        this.maxProposalsPerBatch = Math.max(1, maxProposalsPerBatch);
        this.claimScanSize = Math.max(1, claimScanSize);
        this.recoveryBatchSize = Math.max(1, recoveryBatchSize);
        this.proposalHoldDuration = Duration.ofSeconds(Math.max(0, proposalHoldSeconds));
    }

    /**
     * 返回一批固定提案。领取阶段只做短事务，不把模型推理时间放在数据库锁中。
     */
    @Transactional
    public Optional<MemoryMergeBatchWorkResponse> claimNext(MemoryMergeBatchClaimRequest request) {
        Instant now = Instant.now();
        recoverExpiredBatchesInternal(now);
        // 给同一版本的成功修复留出一个小窗口，避免 Memory Agent 逐条改写同一条经验。
        Instant readyBefore = now.minus(proposalHoldDuration);
        Duration leaseDuration = Duration.ofSeconds(request.leaseSeconds());
        for (UUID memoryId : proposals.findPendingUpdateMemoryIds(
                MemoryProposalStatus.PENDING, MemoryProposalOperation.UPDATE,
                readyBefore,
                PageRequest.of(0, claimScanSize)
        )) {
            Optional<MemoryMergeBatchWorkResponse> claimed = claimUpdateBatch(
                    memoryId, request.workerId(), leaseDuration
            );
            if (claimed.isPresent()) return claimed;
        }
        for (UUID proposalId : proposals.findPendingCreateIds(
                MemoryProposalStatus.PENDING, MemoryProposalOperation.CREATE,
                readyBefore,
                PageRequest.of(0, claimScanSize)
        )) {
            Optional<MemoryMergeBatchWorkResponse> claimed = claimCreateBatch(
                    proposalId, request.workerId(), leaseDuration
            );
            if (claimed.isPresent()) return claimed;
        }
        return Optional.empty();
    }

    @Transactional
    public MemoryMergeBatchCompletionResponse complete(
            UUID batchId, MemoryMergeBatchCompletionRequest request
    ) {
        MemoryMergeBatch batch = batches.findByIdForUpdate(batchId)
                .orElseThrow(() -> new NotFoundException("Memory merge batch not found: " + batchId));
        requireLease(batch, request.workerId(), request.leaseToken());
        List<MemoryProposal> batchProposals = proposals.findByBatchIdForUpdate(
                batch.getId(), MemoryProposalStatus.PROCESSING
        );
        if (batchProposals.isEmpty()) {
            throw new ConflictException("Memory merge batch no longer owns any pending proposals");
        }
        validateCompletion(request);
        return batch.getOperation() == MemoryProposalOperation.CREATE
                ? completeCreate(batch, batchProposals, request)
                : completeUpdate(batch, batchProposals, request);
    }

    @Transactional
    public MemoryMergeBatchCompletionResponse abandon(
            UUID batchId, MemoryMergeBatchAbandonRequest request
    ) {
        MemoryMergeBatch batch = batches.findByIdForUpdate(batchId)
                .orElseThrow(() -> new NotFoundException("Memory merge batch not found: " + batchId));
        requireLease(batch, request.workerId(), request.leaseToken());
        releaseBatch(batch, request.reason());
        return new MemoryMergeBatchCompletionResponse(
                batch.getId(), batch.getStatus(), batch.getMemoryId(), batch.getBaseVersion(), batch.getFailureReason()
        );
    }

    @Transactional
    public int recoverExpiredBatches() {
        return recoverExpiredBatchesInternal(Instant.now());
    }

    private Optional<MemoryMergeBatchWorkResponse> claimUpdateBatch(
            UUID memoryId, String workerId, Duration leaseDuration
    ) {
        SemanticMemory memory = memories.findByIdForUpdate(memoryId).orElse(null);
        if (memory == null) return Optional.empty();
        long currentVersion = memory.getVersion();
        if (batches.existsRunningForMemoryVersion(memoryId, currentVersion, MemoryMergeBatchStatus.RUNNING)) {
            return Optional.empty();
        }

        List<MemoryProposal> pending = proposals.findPendingUpdatesForMemoryForUpdate(
                memoryId, MemoryProposalStatus.PENDING, MemoryProposalOperation.UPDATE
        );
        for (MemoryProposal proposal : pending) {
            if (proposal.getBaseVersion() == null || proposal.getBaseVersion() != currentVersion) {
                proposal.stale(memoryId, currentVersion,
                        "Proposal was based on an older memory revision and cannot be merged automatically");
            }
        }
        List<MemoryProposal> current = pending.stream()
                .filter(proposal -> proposal.getStatus() == MemoryProposalStatus.PENDING)
                .limit(maxProposalsPerBatch)
                .toList();
        if (current.isEmpty()) return Optional.empty();

        MemoryMergeBatch batch = batches.save(new MemoryMergeBatch(
                memory.getId(), memory.getScopeType(), memory.getRepositoryId(),
                MemoryProposalOperation.UPDATE, currentVersion, current.size(), workerId,
                UUID.randomUUID(), leaseDuration
        ));
        current.forEach(proposal -> proposal.processing(batch.getId()));
        return Optional.of(toWork(batch, memory, current));
    }

    private Optional<MemoryMergeBatchWorkResponse> claimCreateBatch(
            UUID proposalId, String workerId, Duration leaseDuration
    ) {
        MemoryProposal proposal = proposals.findByIdForUpdate(proposalId).orElse(null);
        if (proposal == null || proposal.getStatus() != MemoryProposalStatus.PENDING
                || proposal.getOperation() != MemoryProposalOperation.CREATE) {
            return Optional.empty();
        }
        MemoryObservation observation = observations.findById(proposal.getObservationId()).orElse(null);
        if (observation == null) {
            proposal.rejected("The source observation no longer exists");
            return Optional.empty();
        }
        MemoryScopeType scope = proposal.getScopeType() == null ? MemoryScopeType.REPOSITORY : proposal.getScopeType();
        UUID repositoryId = scope == MemoryScopeType.REPOSITORY ? observation.getRepositoryId() : null;
        if (batches.existsRunningCreateForScope(
                MemoryProposalOperation.CREATE, scope, repositoryId, MemoryMergeBatchStatus.RUNNING
        )) {
            return Optional.empty();
        }
        MemoryMergeBatch batch = batches.save(new MemoryMergeBatch(
                null, scope, repositoryId, MemoryProposalOperation.CREATE, null, 1,
                workerId, UUID.randomUUID(), leaseDuration
        ));
        proposal.processing(batch.getId());
        return Optional.of(toWork(batch, null, List.of(proposal)));
    }

    private MemoryMergeBatchCompletionResponse completeUpdate(
            MemoryMergeBatch batch, List<MemoryProposal> batchProposals, MemoryMergeBatchCompletionRequest request
    ) {
        SemanticMemory memory = memories.findByIdForUpdate(batch.getMemoryId())
                .orElseThrow(() -> new NotFoundException("Semantic memory not found: " + batch.getMemoryId()));
        if (!Long.valueOf(memory.getVersion()).equals(batch.getBaseVersion())) {
            markBatchStale(batch, batchProposals, memory.getVersion(),
                    "Memory changed while this batch was being merged");
            return new MemoryMergeBatchCompletionResponse(
                    batch.getId(), batch.getStatus(), memory.getId(), memory.getVersion(), batch.getFailureReason()
            );
        }
        if (request.action() == MemoryMergeAction.NO_CHANGE) {
            batchProposals.forEach(proposal -> proposal.noChange(memory.getId(), memory.getVersion()));
            batch.complete(MemoryMergeBatchStatus.NO_CHANGE);
            return new MemoryMergeBatchCompletionResponse(
                    batch.getId(), batch.getStatus(), memory.getId(), memory.getVersion(), "No new reusable guidance"
            );
        }

        memory.apply(request.content());
        memories.saveAndFlush(memory);
        revisions.save(new MemoryRevision(memory, null, mergeReason(batch, request.changeReason())));
        enqueueIndex(memory);
        ragCache.invalidateScopeAfterCommit(memory.getScopeKey());
        batchProposals.forEach(proposal -> proposal.applied(memory.getId(), memory.getVersion()));
        // 合并期间迟到、仍基于旧版本的提案永远不能自动进入新版本。
        for (MemoryProposal late : proposals.findPendingUpdatesForMemoryForUpdate(
                memory.getId(), MemoryProposalStatus.PENDING, MemoryProposalOperation.UPDATE
        )) {
            late.stale(memory.getId(), memory.getVersion(),
                    "Proposal was submitted against the previous revision after the merge batch started");
        }
        batch.complete(MemoryMergeBatchStatus.APPLIED);
        return new MemoryMergeBatchCompletionResponse(
                batch.getId(), batch.getStatus(), memory.getId(), memory.getVersion(), "Published merged memory revision"
        );
    }

    private MemoryMergeBatchCompletionResponse completeCreate(
            MemoryMergeBatch batch, List<MemoryProposal> batchProposals, MemoryMergeBatchCompletionRequest request
    ) {
        if (request.action() == MemoryMergeAction.NO_CHANGE) {
            batchProposals.forEach(proposal -> proposal.noChangeWithoutMemory("Memory Agent rejected this proposal"));
            batch.complete(MemoryMergeBatchStatus.NO_CHANGE);
            return new MemoryMergeBatchCompletionResponse(
                    batch.getId(), batch.getStatus(), null, null, "No semantic memory was created"
            );
        }
        String scopeKey = batch.getScopeType() == MemoryScopeType.GLOBAL
                ? "GLOBAL" : "REPOSITORY:" + batch.getRepositoryId();
        String fingerprint = MemoryFingerprint.from(request.content());
        SemanticMemory duplicate = memories.findByScopeKeyAndFingerprint(scopeKey, fingerprint).orElse(null);
        if (duplicate != null) {
            batchProposals.forEach(proposal -> proposal.noChange(duplicate.getId(), duplicate.getVersion()));
            batch.complete(MemoryMergeBatchStatus.NO_CHANGE);
            return new MemoryMergeBatchCompletionResponse(
                    batch.getId(), batch.getStatus(), duplicate.getId(), duplicate.getVersion(),
                    "An equivalent memory already exists"
            );
        }
        SemanticMemory memory = memories.saveAndFlush(new SemanticMemory(
                batch.getScopeType(), batch.getRepositoryId(), scopeKey, fingerprint, request.content()
        ));
        revisions.save(new MemoryRevision(memory, batchProposals.get(0).getObservationId(),
                mergeReason(batch, request.changeReason())));
        enqueueIndex(memory);
        ragCache.invalidateScopeAfterCommit(memory.getScopeKey());
        batchProposals.forEach(proposal -> proposal.applied(memory.getId(), memory.getVersion()));
        batch.complete(MemoryMergeBatchStatus.APPLIED);
        return new MemoryMergeBatchCompletionResponse(
                batch.getId(), batch.getStatus(), memory.getId(), memory.getVersion(), "Created semantic memory"
        );
    }

    private int recoverExpiredBatchesInternal(Instant now) {
        int recovered = 0;
        for (UUID batchId : batches.findExpiredIds(
                MemoryMergeBatchStatus.RUNNING, now, PageRequest.of(0, recoveryBatchSize)
        )) {
            MemoryMergeBatch batch = batches.findByIdForUpdate(batchId).orElse(null);
            if (batch == null || !batch.isExpired(now)) continue;
            releaseBatch(batch, "Memory merge lease expired");
            recovered++;
        }
        return recovered;
    }

    private void releaseBatch(MemoryMergeBatch batch, String reason) {
        proposals.findByBatchIdForUpdate(batch.getId(), MemoryProposalStatus.PROCESSING)
                .forEach(MemoryProposal::releaseForRetry);
        batch.fail(reason);
    }

    private void markBatchStale(
            MemoryMergeBatch batch, List<MemoryProposal> batchProposals, long currentVersion, String reason
    ) {
        batchProposals.forEach(proposal -> proposal.stale(batch.getMemoryId(), currentVersion, reason));
        batch.complete(MemoryMergeBatchStatus.STALE);
    }

    private MemoryMergeBatchWorkResponse toWork(
            MemoryMergeBatch batch, SemanticMemory currentMemory, List<MemoryProposal> batchProposals
    ) {
        Map<UUID, MemoryObservation> observationById = new HashMap<>();
        for (MemoryObservation observation : observations.findAllById(
                batchProposals.stream().map(MemoryProposal::getObservationId).toList()
        )) {
            observationById.put(observation.getId(), observation);
        }
        List<MemoryMergeProposalInput> inputs = new ArrayList<>();
        for (MemoryProposal proposal : batchProposals) {
            MemoryObservation observation = observationById.get(proposal.getObservationId());
            inputs.add(new MemoryMergeProposalInput(
                    proposal.getId(), proposal.getObservationId(), proposal.getChangeReason(),
                    readContent(proposal),
                    observation == null ? "" : observation.getProblemSignature(),
                    observation == null ? "" : observation.getPatchSummary(),
                    observation == null ? "" : observation.getValidationSummary()
            ));
        }
        return new MemoryMergeBatchWorkResponse(
                batch.getId(), batch.getLeaseToken(), batch.getLeaseExpiresAt(), batch.getOperation(),
                batch.getMemoryId(), batch.getBaseVersion(), batch.getScopeType(), batch.getRepositoryId(),
                currentMemory == null ? null : MemoryResponse.from(currentMemory), List.copyOf(inputs)
        );
    }

    private void requireLease(MemoryMergeBatch batch, String workerId, UUID leaseToken) {
        if (!batch.isClaimedBy(workerId, leaseToken, Instant.now())) {
            throw new ConflictException("Memory merge batch lease is no longer valid");
        }
    }

    private void validateCompletion(MemoryMergeBatchCompletionRequest request) {
        if (request.action() == MemoryMergeAction.APPLY
                && (request.content() == null || request.changeReason() == null || request.changeReason().isBlank())) {
            throw new BadRequestException("APPLY requires merged content and a change reason");
        }
    }

    private MemoryContent readContent(MemoryProposal proposal) {
        if (proposal.getProposedPayloadJson() == null || proposal.getProposedPayloadJson().isBlank()) return null;
        try {
            return objectMapper.readValue(proposal.getProposedPayloadJson(), MemoryContent.class);
        } catch (JsonProcessingException error) {
            throw new ConflictException("Memory proposal contains unreadable semantic content");
        }
    }

    private void enqueueIndex(SemanticMemory memory) {
        if (!embeddings.isEnabled()) return;
        if (indexJobs.findByMemoryIdAndMemoryRevisionAndEmbeddingProfile(
                memory.getId(), memory.getVersion(), embeddings.profile()
        ).isEmpty()) {
            indexJobs.save(new MemoryIndexJob(memory.getId(), memory.getVersion(), embeddings.profile()));
        }
    }

    private static String mergeReason(MemoryMergeBatch batch, String reason) {
        return "Merged " + batch.getProposalCount() + " validated proposal(s) in batch " + batch.getId()
                + ": " + reason.strip();
    }
}
