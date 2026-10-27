package com.artoria.repoagentbackend.repair;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;

@RestController
@RequestMapping("/api/repair-capacity")
public class RepairAdmissionController {
    private final RepairAdmissionService admission;
    public RepairAdmissionController(RepairAdmissionService admission) { this.admission = admission; }
    @GetMapping
    public RepairAdmissionStatusResponse status(@RequestHeader("X-User-Id") UUID actorId) {
        return admission.status(actorId);
    }
}
