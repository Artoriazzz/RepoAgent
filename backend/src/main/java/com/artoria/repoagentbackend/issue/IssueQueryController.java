package com.artoria.repoagentbackend.issue;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Sort;
import org.springframework.data.web.PageableDefault;
import com.artoria.repoagentbackend.repository.RepositoryProvider;

@RestController
@RequestMapping("/api/issues")
public class IssueQueryController {
    private final RepositoryIssueService issueService;

    public IssueQueryController(RepositoryIssueService issueService) {
        this.issueService = issueService;
    }

    @GetMapping("/{issueId}")
    public IssueResponse get(
            @PathVariable UUID issueId,
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        return issueService.get(issueId, actorId);
    }

    @GetMapping
    public Page<IssueResponse> list(
            @RequestHeader("X-User-Id") UUID actorId,
            @RequestParam(required = false) IssueState state,
            @RequestParam(required = false) RepositoryProvider provider,
            @RequestParam(defaultValue = "false") boolean repairable,
            @PageableDefault(size = 30, sort = "createdAt", direction = Sort.Direction.DESC)
            Pageable pageable
    ) {
        return issueService.listAccessible(actorId, state, provider, repairable, pageable);
    }
}
