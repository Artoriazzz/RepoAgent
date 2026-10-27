package com.artoria.repoagentbackend.repository;

import com.artoria.repoagentbackend.common.ConflictException;
import com.artoria.repoagentbackend.common.NotFoundException;
import com.artoria.repoagentbackend.identity.AppUser;
import com.artoria.repoagentbackend.identity.AppUserService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;

import java.util.UUID;

@Service
public class CodeRepositoryService {
    private final CodeRepositoryJpaRepository repositories;
    private final AppUserService userService;
    private final RepositoryMemberJpaRepository members;
    private final RepositoryAccessService accessService;

    public CodeRepositoryService(
            CodeRepositoryJpaRepository repositories,
            AppUserService userService,
            RepositoryMemberJpaRepository members,
            RepositoryAccessService accessService
    ) {
        this.repositories = repositories;
        this.userService = userService;
        this.members = members;
        this.accessService = accessService;
    }

    @Transactional
    public CodeRepositoryResponse create(UUID ownerUserId, CreateCodeRepositoryRequest request) {
        if (repositories.existsByGithubRepositoryId(request.githubRepositoryId())) {
            throw new ConflictException("GitHub repository is already connected");
        }
        AppUser owner = userService.getRequired(ownerUserId);
        CodeRepository repository = repositories.save(new CodeRepository(
                request.githubRepositoryId(), request.ownerLogin(), request.name(), request.defaultBranch(), owner
        ));
        members.save(new RepositoryMember(repository, owner, RepositoryRole.OWNER));
        return CodeRepositoryResponse.from(repository);
    }

    @Transactional(readOnly = true)
    public CodeRepository getRequired(UUID id) {
        return repositories.findById(id)
                .orElseThrow(() -> new NotFoundException("Repository not found: " + id));
    }

    @Transactional(readOnly = true)
    public CodeRepositoryResponse get(UUID id, UUID actorId) {
        CodeRepository repository = getRequired(id);
        accessService.requireMember(id, actorId);
        return CodeRepositoryResponse.from(repository);
    }

    @Transactional(readOnly = true)
    public Page<CodeRepositoryResponse> list(UUID actorId, Pageable pageable) {
        return repositories.findAccessibleByUserId(actorId, pageable).map(CodeRepositoryResponse::from);
    }

    @Transactional
    public CodeRepository findOrCreateSweBench(UUID ownerUserId, String repositoryName) {
        return repositories.findByProviderAndExternalId(RepositoryProvider.SWE_BENCH, repositoryName)
                .map(repository -> {
                    requireSameOwner(repository, ownerUserId);
                    return repository;
                })
                .orElseGet(() -> {
                    AppUser owner = userService.getRequired(ownerUserId);
                    CodeRepository repository = repositories.save(CodeRepository.sweBench(repositoryName, owner));
                    members.save(new RepositoryMember(repository, owner, RepositoryRole.OWNER));
                    return repository;
                });
    }

    private void requireSameOwner(CodeRepository repository, UUID ownerUserId) {
        if (repository.getOwner() == null || !repository.getOwner().getId().equals(ownerUserId)) {
            throw new ConflictException("SWE-bench repository is already owned by another platform user");
        }
    }
}
