package com.artoria.repoagentbackend.repository;

import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
import org.springframework.data.domain.Sort;

import java.util.UUID;

@RestController
@RequestMapping("/api/repositories")
public class CodeRepositoryController {
    private final CodeRepositoryService repositoryService;
    private final RepositoryMemberService memberService;

    public CodeRepositoryController(CodeRepositoryService repositoryService, RepositoryMemberService memberService) {
        this.repositoryService = repositoryService;
        this.memberService = memberService;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public CodeRepositoryResponse create(
            @RequestHeader("X-User-Id") UUID ownerUserId,
            @Valid @RequestBody CreateCodeRepositoryRequest request
    ) {
        return repositoryService.create(ownerUserId, request);
    }

    @GetMapping
    public Page<CodeRepositoryResponse> list(
            @RequestHeader("X-User-Id") UUID actorId,
            @PageableDefault(size = 20, sort = "createdAt", direction = Sort.Direction.DESC) Pageable pageable
    ) {
        return repositoryService.list(actorId, pageable);
    }

    @GetMapping("/{repositoryId}")
    public CodeRepositoryResponse get(
            @PathVariable UUID repositoryId,
            @RequestHeader("X-User-Id") UUID actorId
    ) {
        return repositoryService.get(repositoryId, actorId);
    }

    @PostMapping("/{repositoryId}/members")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void addMember(
            @PathVariable UUID repositoryId,
            @RequestHeader("X-User-Id") UUID actorId,
            @Valid @RequestBody AddRepositoryMemberRequest request
    ) {
        memberService.addMember(repositoryId, actorId, request);
    }
}
