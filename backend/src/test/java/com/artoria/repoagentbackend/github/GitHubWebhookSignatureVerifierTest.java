package com.artoria.repoagentbackend.github;

import org.junit.jupiter.api.Test;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;

class GitHubWebhookSignatureVerifierTest {

    @Test
    void acceptsOnlyTheMatchingSignature() throws Exception {
        byte[] payload = "{\"action\":\"opened\"}".getBytes(StandardCharsets.UTF_8);
        GitHubWebhookSignatureVerifier verifier = new GitHubWebhookSignatureVerifier("test-secret");

        assertThat(verifier.isValid(payload, signatureFor("test-secret", payload))).isTrue();
        assertThat(verifier.isValid("changed".getBytes(StandardCharsets.UTF_8), signatureFor("test-secret", payload))).isFalse();
        assertThat(verifier.isValid(payload, signatureFor("other-secret", payload))).isFalse();
    }

    private String signatureFor(String secret, byte[] payload) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        StringBuilder signature = new StringBuilder("sha256=");
        for (byte value : mac.doFinal(payload)) {
            signature.append(String.format("%02x", value));
        }
        return signature.toString();
    }
}

