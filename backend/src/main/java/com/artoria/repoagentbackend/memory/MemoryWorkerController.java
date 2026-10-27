package com.artoria.repoagentbackend.memory;

import com.artoria.repoagentbackend.common.ForbiddenException;
import com.artoria.repoagentbackend.repair.RepairWorkerTokenVerifier;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;

@RestController
@RequestMapping("/internal/memories")
public class MemoryWorkerController {
    private final MemoryService memories;
    private final RepairWorkerTokenVerifier tokenVerifier;

    public MemoryWorkerController(MemoryService memories, RepairWorkerTokenVerifier tokenVerifier) {
        this.memories = memories;
        this.tokenVerifier = tokenVerifier;
    }

    @PostMapping("/search")
    public MemorySearchResponse search(
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody MemorySearchRequest request
    ) {
        verify(token);
        return memories.search(request);
    }

    @PostMapping("/observations")
    public MemoryObservationResponse observation(
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody CreateMemoryObservationRequest request
    ) {
        verify(token);
        return memories.recordObservation(request);
    }

    @PostMapping("/proposals")
    public MemoryProposalResponse proposal(
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody CreateMemoryProposalRequest request
    ) {
        verify(token);
        return memories.submitProposal(request);
    }

    @PostMapping("/{memoryId}/feedback")
    public MemoryResponse feedback(
            @PathVariable UUID memoryId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody MemoryUseFeedbackRequest request
    ) {
        verify(token);
        return memories.recordUseFeedback(memoryId, request);
    }

    private void verify(String token) {
        if (!tokenVerifier.isValid(token)) {
            throw new ForbiddenException("Invalid RepoAgent worker token");
        }
    }
}
