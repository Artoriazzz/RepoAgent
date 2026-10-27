package com.artoria.repoagentbackend.repository;

import com.artoria.repoagentbackend.common.ConflictException;
import com.artoria.repoagentbackend.identity.AppUser;
import com.artoria.repoagentbackend.identity.AppUserService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

@Service
public class RepositoryMemberService {
    private final CodeRepositoryService repositoryService;
    private final AppUserService userService;
    private final RepositoryMemberJpaRepository members;
    private final RepositoryAccessService accessService;

    public RepositoryMemberService(
            CodeRepositoryService repositoryService,
            AppUserService userService,
            RepositoryMemberJpaRepository members,
            RepositoryAccessService accessService
    ) {
        this.repositoryService = repositoryService;
        this.userService = userService;
        this.members = members;
        this.accessService = accessService;
    }

    @Transactional
    public void addMember(UUID repositoryId, UUID actorId, AddRepositoryMemberRequest request) {
        accessService.requireOwner(repositoryId, actorId);
        if (members.findByRepositoryIdAndUserId(repositoryId, request.userId()).isPresent()) {
            throw new ConflictException("User is already a repository member");
        }
        CodeRepository repository = repositoryService.getRequired(repositoryId);
        AppUser user = userService.getRequired(request.userId());
        members.save(new RepositoryMember(repository, user, request.role()));
    }
}

