package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.common.ForbiddenException;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Size;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@Validated
@RequestMapping("/internal/workers")
public class WorkerNodeController {
    private final RepairWorkerTokenVerifier tokenVerifier;
    private final RepairTaskService taskService;

    public WorkerNodeController(RepairWorkerTokenVerifier tokenVerifier, RepairTaskService taskService) {
        this.tokenVerifier = tokenVerifier;
        this.taskService = taskService;
    }

    @PostMapping("/{workerId}/heartbeat")
    public WorkerHeartbeatResponse heartbeat(
            @PathVariable @Size(max = 255) String workerId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody WorkerHeartbeatRequest request
    ) {
        if (!tokenVerifier.isValid(token)) {
            throw new ForbiddenException("Invalid RepoAgent worker token");
        }
        return taskService.heartbeat(workerId, request);
    }

    @PostMapping("/{workerId}/registrations")
    public WorkerHeartbeatResponse register(
            @PathVariable @Size(max = 255) String workerId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token,
            @Valid @RequestBody WorkerRegistrationRequest request
    ) {
        if (!tokenVerifier.isValid(token)) {
            throw new ForbiddenException("Invalid RepoAgent worker token");
        }
        return taskService.registerWorker(workerId, request);
    }

    @DeleteMapping("/{workerId}/registrations/{instanceId}")
    public void unregister(
            @PathVariable @Size(max = 255) String workerId,
            @PathVariable @Size(max = 128) String instanceId,
            @RequestHeader("X-RepoAgent-Worker-Token") String token
    ) {
        if (!tokenVerifier.isValid(token)) {
            throw new ForbiddenException("Invalid RepoAgent worker token");
        }
        taskService.unregisterWorker(workerId, instanceId);
    }
}
