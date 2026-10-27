package com.artoria.repoagentbackend.memory;

import java.time.Instant;
import java.util.UUID;

public record MemoryObservationResponse(
        UUID id,
        UUID taskId,
        UUID repositoryId,
        MemoryOutcome outcome,
        String problemSignature,
        Instant createdAt
) {
    static MemoryObservationResponse from(MemoryObservation observation) {
        return new MemoryObservationResponse(
                observation.getId(), observation.getTaskId(), observation.getRepositoryId(),
                observation.getOutcome(), observation.getProblemSignature(), observation.getCreatedAt()
        );
    }
}
