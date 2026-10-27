package com.artoria.repoagentbackend.common;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;

import java.time.Duration;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class RedisJsonCacheTest {
    @Mock ObjectProvider<StringRedisTemplate> provider;
    @Mock StringRedisTemplate redis;
    @Mock ValueOperations<String, String> values;

    private RedisJsonCache cache;

    @BeforeEach
    void setUp() {
        when(provider.getIfAvailable()).thenReturn(redis);
        when(redis.opsForValue()).thenReturn(values);
        cache = new RedisJsonCache(provider, new ObjectMapper().findAndRegisterModules(), "test:v1");
    }

    @Test
    void storesAndReadsJsonWithNamespaceAndTtl() {
        Duration ttl = Duration.ofSeconds(2);
        CacheValue expected = new CacheValue("ready", 3);
        when(values.get("test:v1:overview:user-1"))
                .thenReturn("{\"status\":\"ready\",\"count\":3}");

        cache.put("overview:user-1", expected, ttl);
        Optional<CacheValue> actual = cache.get("overview:user-1", CacheValue.class);

        assertThat(actual).contains(expected);
        verify(values).set(
                "test:v1:overview:user-1",
                "{\"status\":\"ready\",\"count\":3}",
                ttl
        );
    }

    @Test
    void redisFailureFallsBackToCacheMiss() {
        when(values.get("test:v1:overview:user-1"))
                .thenThrow(new IllegalStateException("redis unavailable"));
        doThrow(new IllegalStateException("redis unavailable"))
                .when(values).set(
                        "test:v1:overview:user-1",
                        "{\"status\":\"ready\",\"count\":3}",
                        Duration.ofSeconds(2)
                );

        assertThat(cache.get("overview:user-1", CacheValue.class)).isEmpty();
        cache.put("overview:user-1", new CacheValue("ready", 3), Duration.ofSeconds(2));
    }

    @Test
    void readsMultipleValuesWithOneRedisMget() {
        when(values.multiGet(List.of("test:v1:memory:a", "test:v1:memory:b")))
                .thenReturn(Arrays.asList("{\"status\":\"ready\",\"count\":3}", null));

        Map<String, CacheValue> result = cache.getAll(List.of("memory:a", "memory:b"), CacheValue.class);

        assertThat(result).containsEntry("memory:a", new CacheValue("ready", 3));
        assertThat(result).doesNotContainKey("memory:b");
        verify(values).multiGet(List.of("test:v1:memory:a", "test:v1:memory:b"));
    }

    private record CacheValue(String status, int count) {
    }
}
