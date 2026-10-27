package com.artoria.repoagentbackend.identity;

import com.artoria.repoagentbackend.common.ConflictException;
import com.artoria.repoagentbackend.common.NotFoundException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

@Service
public class AppUserService {
    private final AppUserJpaRepository users;

    public AppUserService(AppUserJpaRepository users) {
        this.users = users;
    }

    @Transactional
    public AppUserResponse create(CreateUserRequest request) {
        if (users.existsByGithubUserId(request.githubUserId()) || users.existsByLogin(request.login())) {
            throw new ConflictException("GitHub user is already registered");
        }
        return AppUserResponse.from(users.save(new AppUser(
                request.githubUserId(), request.login(), request.displayName()
        )));
    }

    @Transactional(readOnly = true)
    public AppUser getRequired(UUID userId) {
        return users.findById(userId)
                .orElseThrow(() -> new NotFoundException("User not found: " + userId));
    }
}

