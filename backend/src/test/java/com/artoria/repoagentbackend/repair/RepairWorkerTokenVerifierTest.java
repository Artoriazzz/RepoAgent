package com.artoria.repoagentbackend.repair;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class RepairWorkerTokenVerifierTest {

    @Test
    void acceptsOnlyAConfiguredMatchingToken() {
        RepairWorkerTokenVerifier verifier = new RepairWorkerTokenVerifier("worker-secret");

        assertThat(verifier.isValid("worker-secret")).isTrue();
        assertThat(verifier.isValid("wrong-secret")).isFalse();
        assertThat(new RepairWorkerTokenVerifier("").isValid("")).isFalse();
    }
}
