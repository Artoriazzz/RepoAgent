package com.artoria.repoagentbackend.memory;

import com.artoria.repoagentbackend.issue.RepositoryIssue;
import com.artoria.repoagentbackend.repair.RepairTask;
import com.artoria.repoagentbackend.repair.RepairTaskJpaRepository;
import com.artoria.repoagentbackend.repository.CodeRepository;
import com.artoria.repoagentbackend.repository.RepositoryProvider;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class MemorySearchServiceTest {
    @Mock MemoryObservationJpaRepository observations;
    @Mock MemoryProposalJpaRepository proposals;
    @Mock SemanticMemoryJpaRepository memories;
    @Mock MemoryRevisionJpaRepository revisions;
    @Mock MemoryIndexJobJpaRepository indexJobs;
    @Mock MemoryRecallJpaRepository recalls;
    @Mock MemoryRecallRecorder recallRecorder;
    @Mock RepairTaskJpaRepository tasks;
    @Mock MemoryEmbeddingClient embeddings;
    @Mock MemoryRagCache ragCache;
    @Mock MemoryQdrantStore qdrant;
    @Mock RepairTask task;
    @Mock RepositoryIssue issue;
    @Mock CodeRepository repository;

    private MemoryService service;
    private UUID taskId;
    private UUID repositoryId;
    private UUID issueId;

    @BeforeEach
    void setUp() {
        taskId = UUID.randomUUID();
        repositoryId = UUID.randomUUID();
        issueId = UUID.randomUUID();
        service = new MemoryService(
                observations, proposals, memories, revisions, indexJobs, recalls, recallRecorder,
                tasks, embeddings, ragCache, qdrant, new ObjectMapper().findAndRegisterModules(), 200
        );
        when(tasks.findWithIssueById(taskId)).thenReturn(Optional.of(task));
        when(task.getIssue()).thenReturn(issue);
        when(issue.getRepository()).thenReturn(repository);
        when(issue.getId()).thenReturn(issueId);
        when(repository.getId()).thenReturn(repositoryId);
        when(repository.getProvider()).thenReturn(RepositoryProvider.GITHUB);
        when(revisions.findMemoryIdsSourcedFromIssue(issueId)).thenReturn(List.of());
    }

    @Test
    void usesStructuredFeaturesAndVersionedCacheForVectorRecall() {
        when(task.getId()).thenReturn(taskId);
        UUID memoryId = UUID.randomUUID();
        MemoryScopeEntry entry = new MemoryScopeEntry(
                memoryId, 3, MemoryScopeType.GLOBAL, "GLOBAL", null, MemoryStatus.ACTIVE,
                0.7, 7, 1, Instant.now()
        );
        MemoryContent content = new MemoryContent(
                "Parser boundary", "Keep reader and writer aligned",
                "Object: parser boundary. Operation: parse. Symptom: one side rejects valid input.",
                "Shared reader/writer boundaries", "Change the common contract",
                "Run a public roundtrip", "Do not patch one side only", "Python", "pandas",
                "parser-contract", null
        );
        MemorySearchRequest request = new MemorySearchRequest(
                taskId, "unstructured issue fallback", "Python", "pandas", "parser-contract", 4, true,
                "parser boundary", "parse", "reader rejects valid input",
                "reader and writer preserve the same contract", "roundtrip input"
        );
        String expectedQuery = "object: parser boundary\noperation: parse\n"
                + "symptom: reader rejects valid input\n"
                + "expected behavior: reader and writer preserve the same contract\n"
                + "trigger conditions: roundtrip input";
        when(embeddings.embed(expectedQuery))
                .thenReturn(Optional.of(new MemoryEmbeddingResult("embed-model", List.of(1.0, 0.0))));
        when(ragCache.findOrLoadScopeEntries(eq("GLOBAL"), any())).thenReturn(List.of(entry));
        when(ragCache.findOrLoadScopeEntries(eq("REPOSITORY:" + repositoryId), any())).thenReturn(List.of());
        when(ragCache.findOrLoadScopeEntries(eq("HISTORICAL_SWE_BENCH"), any())).thenReturn(List.of());
        when(ragCache.findVectors(any())).thenReturn(Map.of(
                memoryId, new MemoryVectorValue("embed-model", 2, List.of(1.0, 0.0))
        ));
        when(ragCache.findContents(any())).thenReturn(Map.of(memoryId, content));
        when(ragCache.embeddingProfile()).thenReturn("memory-retrieval-v1");

        MemorySearchResponse response = service.search(request);

        assertThat(response.vectorSearchUsed()).isTrue();
        assertThat(response.hits()).singleElement().satisfies(hit -> {
            assertThat(hit.memory().id()).isEqualTo(memoryId);
            assertThat(hit.memory().content().retrievalText()).isEqualTo(content.retrievalText());
            assertThat(hit.vectorScore()).isEqualTo(1.0);
            assertThat(hit.matchReason()).isEqualTo("embedding+quality");
        });
        verify(memories, never()).findRecallCandidates(any(), any(), any());
        verify(recallRecorder).record(eq(taskId), any());
    }

    @Test
    void fallsBackToKeywordRecallWhenEmbeddingIsUnavailable() {
        MemorySearchRequest request = new MemorySearchRequest(
                taskId, "parser roundtrip", "Python", "pandas", "parser-contract", 4, false
        );
        SemanticMemory memory = new SemanticMemory(
                MemoryScopeType.GLOBAL, null, "GLOBAL", "fingerprint",
                new MemoryContent(
                        "Parser contract", "Reader and writer share an output boundary",
                        "Parser reader/writer roundtrip contract", "Parser changes affecting both ends",
                        "Change the shared boundary", "Run reader writer roundtrip", null,
                        "Python", "pandas", "parser-contract", null
                )
        );
        when(embeddings.embed("parser roundtrip")).thenReturn(Optional.empty());
        when(memories.findRecallCandidates(any(), eq(MemoryStatus.ACTIVE), any())).thenReturn(List.of(memory));

        MemorySearchResponse response = service.search(request);

        assertThat(response.vectorSearchUsed()).isFalse();
        assertThat(response.hits()).singleElement().extracting(MemorySearchHit::matchReason)
                .isEqualTo("keyword+metadata+quality");
    }

    @Test
    void doesNotRecallHistoricalBenchmarkMemoriesForSWEbenchTasks() {
        when(repository.getProvider()).thenReturn(RepositoryProvider.SWE_BENCH);
        MemorySearchRequest request = new MemorySearchRequest(
                taskId, "reverse relation", "Python", "Django", "validation-contract", 4, false
        );
        when(embeddings.embed("reverse relation")).thenReturn(Optional.of(
                new MemoryEmbeddingResult("embed-model", List.of(1.0, 0.0))
        ));
        when(ragCache.findOrLoadScopeEntries(eq("GLOBAL"), any())).thenReturn(List.of());
        when(ragCache.findOrLoadScopeEntries(eq("REPOSITORY:" + repositoryId), any())).thenReturn(List.of());
        MemorySearchResponse response = service.search(request);

        assertThat(response.hits()).isEmpty();
        verify(ragCache, never()).findOrLoadScopeEntries(eq("HISTORICAL_SWE_BENCH"), any());
    }

    @Test
    void retrievesGlobalAndRepositoryMemoriesFromQdrantAndChecksTheirCurrentRevision() {
        UUID memoryId = UUID.randomUUID();
        MemoryContent content = new MemoryContent(
                "Parser boundary", "Keep reader and writer aligned", "reader writer roundtrip",
                "Shared reader/writer boundaries", "Change the common contract", null, null,
                "Python", "pandas", "parser-contract", null
        );
        SemanticMemory current = new SemanticMemory(
                MemoryScopeType.GLOBAL, null, "GLOBAL", "fingerprint", content
        );
        MemoryResponse payload = new MemoryResponse(
                current.getId(), MemoryScopeType.GLOBAL, "GLOBAL", null, current.getVersion(),
                MemoryStatus.ACTIVE, 0.8, 4, 1, content, "embed-model", "memory-retrieval-v1", 2,
                current.getVersion(), current.getUpdatedAt()
        );
        MemorySearchRequest request = new MemorySearchRequest(
                taskId, "parser roundtrip", "Python", "pandas", "parser-contract", 4, true
        );
        when(embeddings.embed("parser roundtrip"))
                .thenReturn(Optional.of(new MemoryEmbeddingResult("embed-model", List.of(1.0, 0.0))));
        when(qdrant.isEnabled()).thenReturn(true);
        when(qdrant.search(any(), any(), eq(4))).thenReturn(MemoryQdrantQueryResult.success(List.of(
                new MemoryQdrantSearchHit(payload, 0.9, 0.89)
        )));
        when(memories.findAllById(any())).thenReturn(List.of(current));

        MemorySearchResponse response = service.search(request);

        assertThat(response.vectorSearchUsed()).isTrue();
        assertThat(response.hits()).singleElement().satisfies(hit -> {
            assertThat(hit.memory().id()).isEqualTo(current.getId());
            assertThat(hit.matchReason()).isEqualTo("qdrant-embedding+quality");
        });
        verify(ragCache, never()).findScopeEntries(any());
    }

    @Test
    void doesNotScanMysqlCandidatesWhenQdrantReturnsANormalEmptyResult() {
        MemorySearchRequest request = new MemorySearchRequest(
                taskId, "parser roundtrip", "Python", "pandas", "parser-contract", 4, false
        );
        when(embeddings.embed("parser roundtrip"))
                .thenReturn(Optional.of(new MemoryEmbeddingResult("embed-model", List.of(1.0, 0.0))));
        when(qdrant.isEnabled()).thenReturn(true);
        when(qdrant.search(any(), any(), eq(4))).thenReturn(MemoryQdrantQueryResult.success(List.of()));

        MemorySearchResponse response = service.search(request);

        assertThat(response.vectorSearchUsed()).isTrue();
        assertThat(response.retrievalStatus()).isEqualTo(MemoryRetrievalStatus.QDRANT_EMPTY);
        assertThat(response.hits()).isEmpty();
        verify(memories, never()).findRecallCandidates(any(), any(), any());
    }

    @Test
    void degradesToNoMemoryWhenQdrantIsUnavailableInsteadOfCreatingABackendStorm() {
        MemorySearchRequest request = new MemorySearchRequest(
                taskId, "parser roundtrip", "Python", "pandas", "parser-contract", 4, false
        );
        when(embeddings.embed("parser roundtrip"))
                .thenReturn(Optional.of(new MemoryEmbeddingResult("embed-model", List.of(1.0, 0.0))));
        when(qdrant.isEnabled()).thenReturn(true);
        when(qdrant.search(any(), any(), eq(4))).thenReturn(
                MemoryQdrantQueryResult.unavailable("circuit open")
        );

        MemorySearchResponse response = service.search(request);

        assertThat(response.vectorSearchUsed()).isTrue();
        assertThat(response.retrievalStatus()).isEqualTo(MemoryRetrievalStatus.MEMORY_UNAVAILABLE);
        assertThat(response.hits()).isEmpty();
        verify(memories, never()).findRecallCandidates(any(), any(), any());
    }

    @Test
    void acceptsAPrecomputedEmbeddingWithoutCallingTheEmbeddingProviderAgain() {
        MemorySearchRequest request = new MemorySearchRequest(
                taskId, "parser roundtrip", "Python", "pandas", "parser-contract", 4, false
        );
        when(memories.findRecallCandidates(any(), eq(MemoryStatus.ACTIVE), any())).thenReturn(List.of());
        when(ragCache.findOrLoadScopeEntries(eq("GLOBAL"), any())).thenReturn(List.of());
        when(ragCache.findOrLoadScopeEntries(eq("REPOSITORY:" + repositoryId), any())).thenReturn(List.of());
        when(ragCache.findOrLoadScopeEntries(eq("HISTORICAL_SWE_BENCH"), any())).thenReturn(List.of());

        MemorySearchResponse response = service.searchWithEmbedding(
                request, new MemoryEmbeddingResult("embed-model", List.of(1.0, 0.0))
        );

        assertThat(response.vectorSearchUsed()).isFalse();
        verify(embeddings, never()).embed(any());
    }
}
