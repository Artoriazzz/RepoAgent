package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.common.RedisJsonCache;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.transaction.support.TransactionTemplate;

import java.time.Duration;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

class RepairAgentContextStoreTest {
    @Test
    void rollbackDoesNotPublishContextAndCacheMissReadsCommittedHistory() throws Exception {
        var dataSource = new DriverManagerDataSource(
                "jdbc:h2:mem:" + UUID.randomUUID() + ";DB_CLOSE_DELAY=-1", "sa", "");
        var jdbc = new JdbcTemplate(dataSource);
        jdbc.execute("""
                CREATE TABLE repair_agent_context (
                    context_key VARCHAR(112) PRIMARY KEY, task_id CHAR(36) NOT NULL,
                    stage_sequence BIGINT NOT NULL, pipeline_stage VARCHAR(64) NOT NULL,
                    body CLOB NOT NULL, UNIQUE (task_id, stage_sequence)
                )
                """);
        var tx = new TransactionTemplate(new DataSourceTransactionManager(dataSource));
        var cache = mock(RedisJsonCache.class);
        var mapper = new ObjectMapper();
        var store = new RepairAgentContextStore(jdbc, cache, mapper, 60);
        var task = new RepairTask(null, "sha", "repoagent/test", "request");
        task.transitionTo(RepairTaskStatus.QUEUED);
        UUID lease = task.claimPipelineStage(RepairPipelineStage.COORDINATOR, "worker", 90);
        String key = "agent-context:" + task.getId() + ":0:1";
        JsonNode body = mapper.readTree("{\"sessions\":[],\"material\":{\"detail\":\"brief ready\"}}");

        tx.executeWithoutResult(status -> {
            store.archive(task, body);
            status.setRollbackOnly();
        });
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM repair_agent_context", Integer.class)).isZero();
        verifyNoInteractions(cache);

        tx.executeWithoutResult(status -> {
            store.archive(task, body);
            verifyNoInteractions(cache);
        });
        verify(cache).put(key, body, Duration.ofSeconds(60));
        task.handoffPipelineStage(RepairPipelineStage.COORDINATOR, RepairPipelineStage.IMPLEMENT,
                "worker", lease, "brief ready");
        var refs = store.referencesFor(task);
        assertThat(refs.incomingKey()).isEqualTo(key);
        assertThat(refs.ownKey()).isNull();

        when(cache.get(key, JsonNode.class)).thenReturn(Optional.empty());
        assertThat(store.read(key).body()).isEqualTo(body);
        when(cache.get(key, JsonNode.class)).thenReturn(Optional.of(body));
        jdbc.update("DELETE FROM repair_agent_context");
        assertThat(store.read(key).body()).isEqualTo(body);
    }
}
