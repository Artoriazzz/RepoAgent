package com.artoria.repoagentbackend.repair;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.core.StringRedisTemplate;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class WorkerPresenceStoreTest {
    @Mock ObjectProvider<StringRedisTemplate> provider;

    @Test
    void redisOutageDoesNotPretendWorkersAreOffline() {
        when(provider.getIfAvailable()).thenReturn(null);
        WorkerPresenceStore store = new WorkerPresenceStore(
                provider, new ObjectMapper().findAndRegisterModules(), "test:v1", 45
        );

        WorkerPresenceWriteResult result = store.heartbeat(
                "implement-1", "instance-1", 1, "implement", "BUSY",
                UUID.randomUUID(), null, null
        );

        assertThat(result.available()).isFalse();
        assertThat(result.accepted()).isFalse();
        assertThat(store.current().available()).isFalse();
    }
}
