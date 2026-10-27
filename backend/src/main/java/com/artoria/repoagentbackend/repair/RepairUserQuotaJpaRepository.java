package com.artoria.repoagentbackend.repair;
import org.springframework.data.jpa.repository.JpaRepository;
import java.util.UUID;
public interface RepairUserQuotaJpaRepository extends JpaRepository<RepairUserQuota, UUID> { }
