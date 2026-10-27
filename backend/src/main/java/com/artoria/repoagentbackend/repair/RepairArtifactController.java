package com.artoria.repoagentbackend.repair;

import org.springframework.core.io.Resource;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;

@RestController
@RequestMapping("/api/repair-tasks/{taskId}/artifacts")
public class RepairArtifactController {
    private final RepairArtifactService artifactService;

    public RepairArtifactController(RepairArtifactService artifactService) {
        this.artifactService = artifactService;
    }

    @GetMapping
    public List<RepairArtifactResponse> list(
            @PathVariable UUID taskId,
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        return artifactService.list(taskId, actorId);
    }

    @GetMapping("/{type}")
    public ResponseEntity<Resource> download(
            @PathVariable UUID taskId,
            @PathVariable RepairArtifactType type,
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        RepairArtifactService.ArtifactDownload download = artifactService.download(taskId, actorId, type);
        ContentDisposition disposition = ContentDisposition.attachment()
                .filename(download.fileName(), StandardCharsets.UTF_8)
                .build();
        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType(download.mediaType()))
                .header(HttpHeaders.CONTENT_DISPOSITION, disposition.toString())
                .body(download.resource());
    }
}
