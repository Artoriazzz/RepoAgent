package com.artoria.repoagentbackend.benchmark;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import org.springframework.http.HttpStatus;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;

@RestController
@Validated
@RequestMapping("/api/benchmark")
public class SweBenchRepairController {
    private final SweBenchRepairService service;

    public SweBenchRepairController(SweBenchRepairService service) {
        this.service = service;
    }

    @PostMapping("/repair-tasks")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public SweBenchRepairResponse submit(
            @RequestHeader("X-User-Id") UUID actorId,
            @RequestHeader("Idempotency-Key") @NotBlank String idempotencyKey,
            @Valid @RequestBody CreateSweBenchRepairRequest request
    ) {
        return service.submit(actorId, idempotencyKey, request);
    }

    @PostMapping("/issues")
    @ResponseStatus(HttpStatus.CREATED)
    public SweBenchIssueImportResponse importIssue(
            @RequestHeader("X-User-Id") UUID actorId,
            @Valid @RequestBody CreateSweBenchRepairRequest request
    ) {
        return service.importIssue(actorId, request);
    }
}
