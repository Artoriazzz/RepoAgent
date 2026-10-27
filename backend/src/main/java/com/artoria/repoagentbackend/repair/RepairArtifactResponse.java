package com.artoria.repoagentbackend.repair;

public record RepairArtifactResponse(
        RepairArtifactType type,
        String fileName,
        long size,
        boolean available
) {
}
