package com.artoria.repoagentbackend.repository;

import com.artoria.repoagentbackend.common.ForbiddenException;
import com.artoria.repoagentbackend.common.NotFoundException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

@Service
public class RepositoryAccessService {
    private final RepositoryMemberJpaRepository members;

    public RepositoryAccessService(RepositoryMemberJpaRepository members) {
        this.members = members;
    }

    @Transactional(readOnly = true)
    public void requireMember(UUID repositoryId, UUID userId) {
        members.findByRepositoryIdAndUserId(repositoryId, userId)
                .orElseThrow(() -> new NotFoundException("User is not a member of this repository"));
    }

    @Transactional(readOnly = true)
    public void requireOwner(UUID repositoryId, UUID userId) {
        RepositoryMember member = members.findByRepositoryIdAndUserId(repositoryId, userId)
                .orElseThrow(() -> new NotFoundException("User is not a member of this repository"));
        if (member.getRole() != RepositoryRole.OWNER) {
            throw new ForbiddenException("Only a repository owner can perform this operation");
        }
    }
}
