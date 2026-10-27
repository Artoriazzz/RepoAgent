package com.artoria.repoagentbackend.identity;

import java.util.UUID;

public record AppUserResponse(UUID id, long githubUserId, String login, String displayName) {
    static AppUserResponse from(AppUser user) {
        return new AppUserResponse(user.getId(), user.getGithubUserId(), user.getLogin(), user.getDisplayName());
    }
}

