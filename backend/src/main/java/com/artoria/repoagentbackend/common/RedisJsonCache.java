package com.artoria.repoagentbackend.common;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/** Redis 只作为可丢失的加速层；任何故障都会退化为回源查询。 */
@Component
public class RedisJsonCache {
    private static final Logger LOGGER = LoggerFactory.getLogger(RedisJsonCache.class);
    private static final DefaultRedisScript<Long> RELEASE_LOCK_SCRIPT = new DefaultRedisScript<>(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
            Long.class
    );

    private final ObjectProvider<StringRedisTemplate> redis;
    private final ObjectMapper objectMapper;
    private final String prefix;

    public RedisJsonCache(
            ObjectProvider<StringRedisTemplate> redis,
            ObjectMapper objectMapper,
            @org.springframework.beans.factory.annotation.Value("${app.cache.prefix:repoagent:v1}") String prefix
    ) {
        this.redis = redis;
        this.objectMapper = objectMapper;
        this.prefix = prefix.endsWith(":") ? prefix : prefix + ":";
    }

    public <T> Optional<T> get(String key, Class<T> type) {
        return read(key, type);
    }

    /**
     * 读取列表、Map 等泛型 JSON。缓存层无法使用时仍按未命中处理，业务逻辑必须能回源。
     */
    public <T> Optional<T> get(String key, TypeReference<T> type) {
        return read(key, type);
    }

    /**
     * 批量读取同类型 JSON；返回值按调用方传入的逻辑 Key 组织，Redis 连接异常时统一回退为空。
     */
    public <T> Map<String, T> getAll(Collection<String> keys, Class<T> type) {
        if (keys == null || keys.isEmpty()) return Map.of();
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return Map.of();
        List<String> logicalKeys = keys.stream().distinct().toList();
        try {
            List<String> values = template.opsForValue().multiGet(logicalKeys.stream().map(this::fullKey).toList());
            if (values == null || values.isEmpty()) return Map.of();
            Map<String, T> result = new HashMap<>();
            for (int index = 0; index < logicalKeys.size() && index < values.size(); index++) {
                String value = values.get(index);
                if (value == null) continue;
                try {
                    result.put(logicalKeys.get(index), objectMapper.readValue(value, type));
                } catch (JsonProcessingException ignored) {
                    // 单条缓存损坏不应让同批其他命中失效。
                }
            }
            return Map.copyOf(result);
        } catch (RuntimeException error) {
            LOGGER.debug("Redis cache batch read failed for {} keys", keys.size(), error);
            return Map.of();
        }
    }

    private <T> Optional<T> read(String key, Class<T> type) {
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return Optional.empty();
        try {
            String value = template.opsForValue().get(fullKey(key));
            return value == null ? Optional.empty() : Optional.of(objectMapper.readValue(value, type));
        } catch (RuntimeException | JsonProcessingException error) {
            LOGGER.debug("Redis cache read failed for key={}", key, error);
            return Optional.empty();
        }
    }

    private <T> Optional<T> read(String key, TypeReference<T> type) {
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return Optional.empty();
        try {
            String value = template.opsForValue().get(fullKey(key));
            return value == null ? Optional.empty() : Optional.of(objectMapper.readValue(value, type));
        } catch (RuntimeException | JsonProcessingException error) {
            LOGGER.debug("Redis cache read failed for key={}", key, error);
            return Optional.empty();
        }
    }

    public void put(String key, Object value, Duration ttl) {
        if (ttl.isZero() || ttl.isNegative()) return;
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return;
        try {
            template.opsForValue().set(fullKey(key), objectMapper.writeValueAsString(value), ttl);
        } catch (RuntimeException | JsonProcessingException error) {
            LOGGER.debug("Redis cache write failed for key={}", key, error);
        }
    }

    public void evict(String key) {
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return;
        try {
            template.delete(fullKey(key));
        } catch (RuntimeException error) {
            LOGGER.debug("Redis cache eviction failed for key={}", key, error);
        }
    }

    /**
     * 仅用于缓存回填的短租约锁。调用方必须携带随机 token 释放，避免过期持有者删掉
     * 新持有者的锁。Redis 不可用时返回 false，业务层可以选择快速降级而非回源风暴。
     */
    public boolean tryAcquireLock(String key, String token, Duration ttl) {
        if (token == null || token.isBlank() || ttl.isZero() || ttl.isNegative()) return false;
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return false;
        try {
            return Boolean.TRUE.equals(template.opsForValue().setIfAbsent(fullKey(key), token, ttl));
        } catch (RuntimeException error) {
            LOGGER.debug("Redis cache lock acquisition failed for key={}", key, error);
            return false;
        }
    }

    public void releaseLock(String key, String token) {
        if (token == null || token.isBlank()) return;
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return;
        try {
            template.execute(RELEASE_LOCK_SCRIPT, List.of(fullKey(key)), token);
        } catch (RuntimeException error) {
            LOGGER.debug("Redis cache lock release failed for key={}", key, error);
        }
    }

    private String fullKey(String key) {
        return prefix + key;
    }
}
