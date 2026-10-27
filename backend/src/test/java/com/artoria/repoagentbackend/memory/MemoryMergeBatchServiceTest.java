package com.artoria.repoagentbackend.memory;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.domain.Pageable;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class MemoryMergeBatchServiceTest {
    @Mock MemoryProposalJpaRepository proposals;
    @Mock MemoryMergeBatchJpaRepository batches;
    @Mock SemanticMemoryJpaRepository memories;
    @Mock MemoryObservationJpaRepository observations;
    @Mock MemoryRevisionJpaRepository revisions;
    @Mock MemoryIndexJobJpaRepository indexJobs;
    @Mock MemoryEmbeddingClient embeddings;
    @Mock MemoryRagCache ragCache;

    private MemoryMergeBatchService service;
    private SemanticMemory memory;
    private MemoryProposal first;
    private MemoryProposal late;
    private MemoryObservation observation;

    @BeforeEach
    void setUp() {
        memory = new SemanticMemory(
                MemoryScopeType.GLOBAL, null, "GLOBAL", "parser-contract",
                content("original strategy")
        );
        observation = new MemoryObservation(
                UUID.randomUUID(), UUID.randomUUID(), "observation-1", MemoryOutcome.RESOLVED,
                "parser boundary", "shared contract change", "public regression passed", "{}"
        );
        first = proposal("proposal-1", "first improvement");
        late = proposal("proposal-2", "late improvement");
        service = new MemoryMergeBatchService(
                proposals, batches, memories, observations, revisions, indexJobs, embeddings, ragCache,
                new ObjectMapper(), 12, 24, 50, 0
        );
        when(batches.save(any())).thenAnswer(invocation -> invocation.getArgument(0));
        when(embeddings.isEnabled()).thenReturn(false);
        when(observations.findAllById(any())).thenReturn(List.of(observation));
    }

    @Test
    void publishesOnlyTheClaimedVersionAndMarksLateOldVersionProposalsStale() {
        when(proposals.findPendingUpdateMemoryIds(
                eq(MemoryProposalStatus.PENDING), eq(MemoryProposalOperation.UPDATE), any(), any(Pageable.class)
        )).thenReturn(List.of(memory.getId()));
        when(memories.findByIdForUpdate(memory.getId())).thenReturn(Optional.of(memory));
        when(batches.existsRunningForMemoryVersion(
                memory.getId(), memory.getVersion(), MemoryMergeBatchStatus.RUNNING
        )).thenReturn(false);
        when(proposals.findPendingUpdatesForMemoryForUpdate(
                memory.getId(), MemoryProposalStatus.PENDING, MemoryProposalOperation.UPDATE
        )).thenReturn(List.of(first), List.of(late));

        MemoryMergeBatchWorkResponse work = service.claimNext(
                new MemoryMergeBatchClaimRequest("memory-merge-1", 120)
        ).orElseThrow();

        assertThat(first.getStatus()).isEqualTo(MemoryProposalStatus.PROCESSING);
        assertThat(work.baseVersion()).isEqualTo(1);
        // 领取接口返回的 batch 是持久化实体；Mockito 下取回它以模拟下一次请求再次锁定。
        MemoryMergeBatch batch = findSavedBatch();
        when(batches.findByIdForUpdate(batch.getId())).thenReturn(Optional.of(batch));
        when(proposals.findByBatchIdForUpdate(batch.getId(), MemoryProposalStatus.PROCESSING))
                .thenReturn(List.of(first));
        when(memories.saveAndFlush(memory)).thenReturn(memory);

        MemoryMergeBatchCompletionResponse completed = service.complete(
                batch.getId(),
                new MemoryMergeBatchCompletionRequest(
                        "memory-merge-1", work.leaseToken(), MemoryMergeAction.APPLY,
                        content("merged strategy"), "adds a reusable validation boundary"
                )
        );

        assertThat(completed.status()).isEqualTo(MemoryMergeBatchStatus.APPLIED);
        assertThat(memory.getVersion()).isEqualTo(2);
        assertThat(first.getStatus()).isEqualTo(MemoryProposalStatus.APPLIED);
        assertThat(late.getStatus()).isEqualTo(MemoryProposalStatus.STALE);
        verify(revisions).save(any(MemoryRevision.class));
    }

    private MemoryMergeBatch findSavedBatch() {
        org.mockito.ArgumentCaptor<MemoryMergeBatch> captor = org.mockito.ArgumentCaptor.forClass(MemoryMergeBatch.class);
        verify(batches).save(captor.capture());
        return captor.getValue();
    }

    private MemoryProposal proposal(String idempotencyKey, String strategy) {
        return new MemoryProposal(
                memory.getId(), observation.getId(), idempotencyKey, MemoryProposalOperation.UPDATE, 1L,
                null, "validated repair evidence", write(content(strategy))
        );
    }

    private String write(MemoryContent content) {
        try {
            return new ObjectMapper().writeValueAsString(content);
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
    }

    private MemoryContent content(String strategy) {
        return new MemoryContent(
                "Parser boundary", "Keep producer and consumer contracts aligned", "parser contract boundary",
                "Shared parser behavior", strategy, "Run a public roundtrip", "Do not patch one side only",
                "Python", "pandas", "parser-contract", null
        );
    }
}
