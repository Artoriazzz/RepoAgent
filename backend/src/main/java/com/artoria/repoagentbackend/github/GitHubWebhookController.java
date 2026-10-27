package com.artoria.repoagentbackend.github;

import com.artoria.repoagentbackend.common.ForbiddenException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/integrations/github")
public class GitHubWebhookController {
    private final GitHubWebhookSignatureVerifier signatureVerifier;
    private final GitHubWebhookService webhookService;

    public GitHubWebhookController(
            GitHubWebhookSignatureVerifier signatureVerifier,
            GitHubWebhookService webhookService
    ) {
        this.signatureVerifier = signatureVerifier;
        this.webhookService = webhookService;
    }

    @PostMapping("/webhooks")
    public ResponseEntity<Void> receive(
            @RequestHeader("X-GitHub-Delivery") String deliveryId,
            @RequestHeader("X-GitHub-Event") String eventType,
            @RequestHeader("X-Hub-Signature-256") String signature,
            @RequestBody byte[] payload
    ) {
        if (!signatureVerifier.isValid(payload, signature)) {
            throw new ForbiddenException("Invalid GitHub webhook signature");
        }
        webhookService.accept(deliveryId, eventType, payload);
        return ResponseEntity.status(HttpStatus.NO_CONTENT).build();
    }
}

