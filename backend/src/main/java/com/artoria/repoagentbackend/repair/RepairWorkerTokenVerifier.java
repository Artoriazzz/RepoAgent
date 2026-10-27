package com.artoria.repoagentbackend.repair;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

@Component
public class RepairWorkerTokenVerifier {
    private final byte[] configuredToken;

    public RepairWorkerTokenVerifier(@Value("${app.repair.worker-token:}") String configuredToken) {
        this.configuredToken = configuredToken.getBytes(StandardCharsets.UTF_8);
    }

    public boolean isValid(String candidate) {
        return configuredToken.length > 0
                && candidate != null
                && MessageDigest.isEqual(configuredToken, candidate.getBytes(StandardCharsets.UTF_8));
    }
}
