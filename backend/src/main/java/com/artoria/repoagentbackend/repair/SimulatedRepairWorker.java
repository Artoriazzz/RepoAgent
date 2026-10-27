package com.artoria.repoagentbackend.repair;

import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.orm.ObjectOptimisticLockingFailureException;
import org.springframework.stereotype.Component;

@Component
public class SimulatedRepairWorker {
    private final RepairTaskService taskService;

    public SimulatedRepairWorker(RepairTaskService taskService) {
        this.taskService = taskService;
    }

    @RabbitListener(queues = RabbitRepairConfiguration.QUEUE, autoStartup = "${app.repair.simulator-enabled:false}")
    public void consume(RepairTaskMessage message) {
        try {
            taskService.completeSimulatedRun(message.taskId());
        } catch (ObjectOptimisticLockingFailureException ignored) {
            // 并发投递已先一步完成同一任务。
        }
    }
}
