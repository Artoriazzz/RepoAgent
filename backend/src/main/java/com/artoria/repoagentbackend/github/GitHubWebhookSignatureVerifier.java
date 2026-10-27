package com.artoria.repoagentbackend.github;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

@Component
public class GitHubWebhookSignatureVerifier {
    private static final String PREFIX = "sha256=";
    private final byte[] secret;

    public GitHubWebhookSignatureVerifier(@Value("${app.github.webhook-secret:}") String secret) {
        this.secret = secret.getBytes(StandardCharsets.UTF_8);
    }

    public boolean isValid(byte[] payload, String signature) {
        if (secret.length == 0 || signature == null || !signature.startsWith(PREFIX)) {
            return false;
        }
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            byte[] expected = (PREFIX + toHex(mac.doFinal(payload))).getBytes(StandardCharsets.US_ASCII);
            return MessageDigest.isEqual(expected, signature.getBytes(StandardCharsets.US_ASCII));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to verify GitHub webhook signature", exception);
        }
    }

    private String toHex(byte[] bytes) {
        StringBuilder builder = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            builder.append(String.format("%02x", value));
        }
        return builder.toString();
    }
}

