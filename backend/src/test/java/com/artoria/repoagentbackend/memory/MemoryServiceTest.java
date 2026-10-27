package com.artoria.repoagentbackend.memory;

import com.artoria.repoagentbackend.repair.RepairTaskJpaRepository;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class MemoryServiceTest {
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

    private MemoryService service;
    private MemoryObservation observation;
    private SemanticMemory memory;

    @BeforeEach
    void setUp() {
        UUID repositoryId = UUID.randomUUID();
        observation = new MemoryObservation(
                UUID.randomUUID(), repositoryId, "observation-1", MemoryOutcome.RESOLVED,
                "parser rejects nullable numeric dtype", "move conversion before validation",
                "public regression tests passed", "{}"
        );
        memory = new SemanticMemory(
                MemoryScopeType.GLOBAL, null, "GLOBAL", "fingerprint",
                content("original strategy")
        );
        service = new MemoryService(
                observations, proposals, memories, revisions, indexJobs, recalls, recallRecorder,
                tasks, embeddings, ragCache, qdrant, new ObjectMapper(), 200
        );
        when(proposals.findByIdempotencyKey(any())).thenReturn(Optional.empty());
        when(proposals.save(any())).thenAnswer(invocation -> invocation.getArgument(0));
        when(observations.findById(observation.getId())).thenReturn(Optional.of(observation));
    }

    @Test
    void staleProposalReturnsCurrentRevisionWithoutChangingMemory() {
        when(memories.findByIdForUpdate(memory.getId())).thenReturn(Optional.of(memory));

        MemoryProposalResponse response = service.submitProposal(updateRequest(0, "stale strategy"));

        assertThat(response.status()).isEqualTo(MemoryProposalStatus.STALE);
        assertThat(response.appliedRevision()).isEqualTo(1);
        assertThat(memory.getRepairStrategy()).isEqualTo("original strategy");
        verify(memories, never()).saveAndFlush(any());
        verify(revisions, never()).save(any());
    }

    @Test
    void currentProposalIsStagedUntilTheMemoryMergeWorkerPublishesIt() {
        when(memories.findByIdForUpdate(memory.getId())).thenReturn(Optional.of(memory));

        MemoryProposalResponse response = service.submitProposal(updateRequest(1, "improved strategy"));

        assertThat(response.status()).isEqualTo(MemoryProposalStatus.PENDING);
        assertThat(response.appliedRevision()).isNull();
        assertThat(memory.getVersion()).isEqualTo(1);
        assertThat(memory.getRepairStrategy()).isEqualTo("original strategy");
        verify(revisions, never()).save(any(MemoryRevision.class));
        verify(indexJobs, never()).save(any());
    }

    private CreateMemoryProposalRequest updateRequest(long baseVersion, String strategy) {
        return new CreateMemoryProposalRequest(
                observation.getId(), "proposal-" + baseVersion, MemoryProposalOperation.UPDATE,
                memory.getId(), baseVersion, null, content(strategy), "new successful evidence"
        );
    }

    private MemoryContent content(String strategy) {
        return new MemoryContent(
                "Validate producer/consumer contract", "Keep conversion semantics aligned",
                "Nullable numeric dtype crosses an array validation boundary",
                "Nullable numeric dtype crosses a shared validation boundary", strategy,
                "Run focused public regression tests", "Do not use broad hasattr guards",
                "Python", "scikit-learn", "validation-contract", null
        );
    }
}
