package com.artoria.repoagentbackend.repair;

public record ClearRepairTasksResponse(int deleted, int skippedActive) {
}
