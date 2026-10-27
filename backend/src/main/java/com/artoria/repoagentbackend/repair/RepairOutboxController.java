package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.common.ForbiddenException;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;

@RestController
@RequestMapping("/internal/repair-outbox-events")
public class RepairOutboxController {
    private final RepairWorkerTokenVerifier tokenVerifier;
    private final RepairOutboxService outbox;

    public RepairOutboxController(RepairWorkerTokenVerifier tokenVerifier, RepairOutboxService outbox) {
        this.tokenVerifier = tokenVerifier;
        this.outbox = outbox;
    }

    @GetMapping
    public Page<RepairOutboxResponse> list(
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @RequestParam(required = false) RepairOutboxStatus status,
            @PageableDefault(size = 50, sort = "createdAt") Pageable pageable
    ) {
        verify(token);
        return outbox.list(status, pageable);
    }

    @PostMapping("/{eventId}/retry")
    public RepairOutboxResponse retry(
            @PathVariable UUID eventId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token
    ) {
        verify(token);
        return outbox.retry(eventId);
    }

    private void verify(String token) {
        if (!tokenVerifier.isValid(token)) {
            throw new ForbiddenException("Invalid RepoAgent worker token");
        }
    }
}
