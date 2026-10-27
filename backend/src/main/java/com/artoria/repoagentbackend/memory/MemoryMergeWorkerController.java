package com.artoria.repoagentbackend.memory;

import com.artoria.repoagentbackend.common.ForbiddenException;
import com.artoria.repoagentbackend.repair.RepairWorkerTokenVerifier;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;

/** 供独立的 Memory Agent 轮询、合并并提交固定批次。 */
@RestController
@RequestMapping("/internal/memory-merge-batches")
public class MemoryMergeWorkerController {
    private final MemoryMergeBatchService batches;
    private final RepairWorkerTokenVerifier tokenVerifier;

    public MemoryMergeWorkerController(MemoryMergeBatchService batches, RepairWorkerTokenVerifier tokenVerifier) {
        this.batches = batches;
        this.tokenVerifier = tokenVerifier;
    }

    @PostMapping("/claim")
    public ResponseEntity<MemoryMergeBatchWorkResponse> claim(
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody MemoryMergeBatchClaimRequest request
    ) {
        verify(token);
        return batches.claimNext(request).map(ResponseEntity::ok).orElseGet(() -> ResponseEntity.noContent().build());
    }

    @PostMapping("/{batchId}/complete")
    public MemoryMergeBatchCompletionResponse complete(
            @PathVariable UUID batchId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody MemoryMergeBatchCompletionRequest request
    ) {
        verify(token);
        return batches.complete(batchId, request);
    }

    @PostMapping("/{batchId}/abandon")
    public MemoryMergeBatchCompletionResponse abandon(
            @PathVariable UUID batchId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody MemoryMergeBatchAbandonRequest request
    ) {
        verify(token);
        return batches.abandon(batchId, request);
    }

    private void verify(String token) {
        if (!tokenVerifier.isValid(token)) {
            throw new ForbiddenException("Invalid RepoAgent worker token");
        }
    }
}
