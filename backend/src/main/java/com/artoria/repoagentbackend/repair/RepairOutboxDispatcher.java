package com.artoria.repoagentbackend.repair;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Instant;
import java.util.EnumSet;
import java.util.List;

@Component
public class RepairOutboxDispatcher {
    private static final Logger LOGGER = LoggerFactory.getLogger(RepairOutboxDispatcher.class);
    private final RepairOutboxJpaRepository outbox;
    private final RabbitTemplate rabbitTemplate;
    private final ObjectMapper objectMapper;
    private final RepairRuntimeMetrics metrics;
    private final String exchange;
    private final int batchSize;
    private final int maxAttempts;
    private final long confirmTimeoutMillis;

    public RepairOutboxDispatcher(
            RepairOutboxJpaRepository outbox,
            RabbitTemplate rabbitTemplate,
            ObjectMapper objectMapper,
            RepairRuntimeMetrics metrics,
            @Value("${app.repair.outbox.batch-size:20}") int batchSize,
            @Value("${app.repair.outbox.max-attempts:12}") int maxAttempts,
            @Value("${app.repair.outbox.confirm-timeout-millis:5000}") long confirmTimeoutMillis,
            @Value("${app.repair.exchange}") String exchange
    ) {
        this.outbox = outbox;
        this.rabbitTemplate = rabbitTemplate;
        this.objectMapper = objectMapper;
        this.metrics = metrics;
        this.batchSize = batchSize;
        this.maxAttempts = maxAttempts;
        this.confirmTimeoutMillis = confirmTimeoutMillis;
        this.exchange = exchange;
    }

    @Scheduled(fixedDelayString = "${app.repair.outbox.poll-interval-millis:1000}")
    @Transactional
    public void dispatch() {
        List<RepairOutboxEvent> events = outbox.findDispatchable(
                EnumSet.of(RepairOutboxStatus.PENDING, RepairOutboxStatus.FAILED),
                Instant.now(), PageRequest.of(0, batchSize)
        );
        for (RepairOutboxEvent event : events) {
            try {
                RepairTaskMessage message = deserialize(event.getPayloadJson());
                rabbitTemplate.invoke(operations -> {
                    operations.convertAndSend(exchange, message.effectiveStage().routingKey(), message);
                    operations.waitForConfirmsOrDie(confirmTimeoutMillis);
                    return null;
                });
                event.markPublished();
                metrics.outboxPublished();
                LOGGER.info("Published repair outbox event id={} taskId={} attempts={}",
                        event.getId(), event.getAggregateId(), event.getAttempts());
            } catch (Exception error) {
                event.markFailed(error.getMessage(), maxAttempts);
                metrics.outboxFailed();
                LOGGER.warn("Failed to publish repair outbox event id={} taskId={} status={} attempts={}",
                        event.getId(), event.getAggregateId(), event.getStatus(), event.getAttempts(), error);
            }
        }
    }

    private RepairTaskMessage deserialize(String payload) throws JsonProcessingException {
        return objectMapper.readValue(payload, RepairTaskMessage.class);
    }
}
