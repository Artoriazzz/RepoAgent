package com.artoria.repoagentbackend.repair;

import java.time.Instant;
import java.util.UUID;

public record RepairTaskEventResponse(
        UUID id,
        RepairTaskEventType eventType,
        String stage,
        String message,
        String metadataJson,
        Instant createdAt
) {
    public static RepairTaskEventResponse from(RepairTaskEvent event) {
        return new RepairTaskEventResponse(
                event.getId(),
                event.getEventType(),
                event.getStage(),
                event.getMessage(),
                event.getMetadataJson(),
                event.getCreatedAt()
        );
    }
}
