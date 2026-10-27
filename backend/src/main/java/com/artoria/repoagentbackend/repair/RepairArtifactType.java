package com.artoria.repoagentbackend.repair;

public enum RepairArtifactType {
    PATCH("model.patch", "text/x-diff"),
    TRAJECTORY("trajectory.json", "application/json"),
    VALIDATION("validation.json", "application/json"),
    AGENT_LOG("repo_agent.log", "text/plain"),
    HARNESS_LOG(null, "text/plain");

    private final String siblingName;
    private final String mediaType;

    RepairArtifactType(String siblingName, String mediaType) {
        this.siblingName = siblingName;
        this.mediaType = mediaType;
    }

    public String siblingName() { return siblingName; }
    public String mediaType() { return mediaType; }
}
