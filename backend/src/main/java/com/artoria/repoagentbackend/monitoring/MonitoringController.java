package com.artoria.repoagentbackend.monitoring;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.UUID;

@RestController
@RequestMapping("/api/monitoring")
public class MonitoringController {
    private final MonitoringService monitoring;

    public MonitoringController(MonitoringService monitoring) {
        this.monitoring = monitoring;
    }

    @GetMapping("/overview")
    public MonitoringOverviewResponse overview(@RequestHeader("X-User-Id") UUID actorId) {
        return monitoring.overview(actorId);
    }

    @GetMapping("/workers")
    public List<MonitoringWorkerResponse> workers(@RequestHeader("X-User-Id") UUID actorId) {
        return monitoring.workers(actorId);
    }
}
