package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.common.NotFoundException;
import com.artoria.repoagentbackend.repository.RepositoryAccessService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;

@Service
public class RepairArtifactService {
    private final RepairTaskJpaRepository tasks;
    private final RepositoryAccessService accessService;
    private final Path artifactRoot;

    public RepairArtifactService(
            RepairTaskJpaRepository tasks,
            RepositoryAccessService accessService,
            @Value("${app.repair.artifact-root:/var/lib/repo-agent/runs}") String artifactRoot
    ) {
        this.tasks = tasks;
        this.accessService = accessService;
        this.artifactRoot = Path.of(artifactRoot).toAbsolutePath().normalize();
    }

    @Transactional(readOnly = true)
    public List<RepairArtifactResponse> list(UUID taskId, UUID actorId) {
        RepairTask task = getAccessible(taskId, actorId);
        return Arrays.stream(RepairArtifactType.values()).map(type -> describe(task, type)).toList();
    }

    @Transactional(readOnly = true)
    public ArtifactDownload download(UUID taskId, UUID actorId, RepairArtifactType type) {
        RepairTask task = getAccessible(taskId, actorId);
        Path path = resolve(task, type);
        if (path == null || !Files.isRegularFile(path)) {
            throw new NotFoundException("Repair artifact is not available: " + type);
        }
        return new ArtifactDownload(new FileSystemResource(path), path.getFileName().toString(), type.mediaType());
    }

    private RepairTask getAccessible(UUID taskId, UUID actorId) {
        RepairTask task = tasks.findWithIssueById(taskId)
                .orElseThrow(() -> new NotFoundException("Repair task not found: " + taskId));
        accessService.requireMember(task.getIssue().getRepository().getId(), actorId);
        return task;
    }

    private RepairArtifactResponse describe(RepairTask task, RepairArtifactType type) {
        Path path;
        try {
            path = resolve(task, type);
        } catch (NotFoundException ignored) {
            return new RepairArtifactResponse(type, null, 0, false);
        }
        boolean available = path != null && Files.isRegularFile(path);
        long size = 0;
        if (available) {
            try {
                size = Files.size(path);
            } catch (IOException ignored) {
                available = false;
            }
        }
        return new RepairArtifactResponse(
                type, path == null ? null : path.getFileName().toString(), size, available
        );
    }

    private Path resolve(RepairTask task, RepairArtifactType type) {
        String storedPath = type == RepairArtifactType.HARNESS_LOG
                ? task.getHarnessLogPath() : task.getPatchArtifactPath();
        if (storedPath == null || storedPath.isBlank()) return null;
        Path base = checked(Path.of(storedPath));
        if (type == RepairArtifactType.PATCH || type == RepairArtifactType.HARNESS_LOG) return base;
        Path parent = base.getParent();
        return parent == null ? null : checked(parent.resolve(type.siblingName()));
    }

    private Path checked(Path path) {
        Path normalized = path.toAbsolutePath().normalize();
        Path checkedRoot = realPathIfPresent(artifactRoot);
        Path checkedPath = realPathIfPresent(normalized);
        if (!checkedPath.startsWith(checkedRoot)) {
            throw new NotFoundException("Repair artifact is outside the configured artifact root");
        }
        return checkedPath;
    }

    private Path realPathIfPresent(Path path) {
        if (!Files.exists(path)) return path;
        try {
            return path.toRealPath();
        } catch (IOException exception) {
            throw new NotFoundException("Repair artifact path cannot be resolved");
        }
    }

    public record ArtifactDownload(Resource resource, String fileName, String mediaType) {
    }
}
