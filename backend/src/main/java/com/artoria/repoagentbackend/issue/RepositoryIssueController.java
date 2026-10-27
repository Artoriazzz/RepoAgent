package com.artoria.repoagentbackend.issue;

import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.ResponseStatus;

import java.util.UUID;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
import org.springframework.data.domain.Sort;

@RestController
@RequestMapping("/api/repositories/{repositoryId}/issues")
public class RepositoryIssueController {
    private final RepositoryIssueService issueService;

    public RepositoryIssueController(RepositoryIssueService issueService) {
        this.issueService = issueService;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public IssueResponse create(
            @PathVariable UUID repositoryId,
            @RequestHeader("X-User-Id") UUID actorId,
            @Valid @RequestBody CreateIssueRequest request
    ) {
        return issueService.create(repositoryId, actorId, request);
    }

    @GetMapping
    public Page<IssueResponse> list(
            @PathVariable UUID repositoryId,
            @RequestHeader("X-User-Id") UUID actorId,
            @PageableDefault(size = 20, sort = "createdAt", direction = Sort.Direction.DESC) Pageable pageable
    ) {
        return issueService.list(repositoryId, actorId, pageable);
    }
}
