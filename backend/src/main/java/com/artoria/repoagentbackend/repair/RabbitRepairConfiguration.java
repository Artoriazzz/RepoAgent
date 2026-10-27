package com.artoria.repoagentbackend.repair;

import org.springframework.amqp.core.Declarables;
import org.springframework.amqp.core.Declarable;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.support.converter.Jackson2JsonMessageConverter;
import org.springframework.amqp.support.converter.MessageConverter;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.ArrayList;
import java.util.List;

@Configuration
public class RabbitRepairConfiguration {
    /** Kept for the simulator and old clients; the coordinator is now the entry queue. */
    public static final String QUEUE = "repoagent.repair.stage.coordinator";

    @Bean
    DirectExchange repairExchange() {
        return new DirectExchange("repoagent.repair", true, false);
    }

    @Bean
    Declarables repairPipelineQueues(DirectExchange repairExchange) {
        List<Declarable> declarations = new ArrayList<>();
        for (RepairPipelineStage stage : RepairPipelineStage.all()) {
            Queue queue = new Queue(stage.queueName(), true);
            declarations.add(queue);
            declarations.add(
                    org.springframework.amqp.core.BindingBuilder.bind(queue)
                            .to(repairExchange)
                            .with(stage.routingKey())
            );
        }
        return new Declarables(declarations);
    }

    @Bean
    MessageConverter messageConverter() {
        return new Jackson2JsonMessageConverter();
    }
}
