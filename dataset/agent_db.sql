CREATE DATABASE IF NOT EXISTS agent_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 1. 先创建用户表
CREATE TABLE IF NOT EXISTS `users` (
  `user_id` varchar(50) NOT NULL COMMENT '用户ID',
  `username` varchar(100) NOT NULL COMMENT '用户名',
  `password` varchar(255) NOT NULL COMMENT '密码哈希',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `last_login_at` timestamp NULL DEFAULT NULL COMMENT '最近登录时间',
  `current_llm_id` int DEFAULT NULL COMMENT '当前使用的模型ID，NULL=使用系统默认模型，关联llms.id',
  PRIMARY KEY (`user_id`),
  UNIQUE KEY `username` (`username`),
  KEY `idx_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户表';

-- 2. 再创建LLM信息表（依赖用户表）
CREATE TABLE IF NOT EXISTS `llms` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `url` varchar(500) NOT NULL COMMENT 'LLM API URL',
  `api_key` varchar(500) NOT NULL COMMENT 'API密钥',
  `user_id` varchar(50) NOT NULL COMMENT '用户ID',
  `model_name` varchar(100) NOT NULL COMMENT '模型名称',
  `model_type` enum('text','image','multimodal','embedding') NOT NULL COMMENT '模型类型',
  `is_deleted` tinyint(1) DEFAULT '0' COMMENT '是否删除',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `state` tinyint(1) unsigned zerofill DEFAULT '1' COMMENT '状态，1 启用；0 未启用; -1 已删除',
  `temperature` float(10,2) DEFAULT '0.50' COMMENT '控制模型输出的随机性，数组越高，回答越有创意',
  PRIMARY KEY (`id`),
  KEY `idx_user_id` (`user_id`),
  CONSTRAINT `llms_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='LLM信息表';

-- users.current_llm_id → llms.id 外键（须在 llms 建表后添加，避免循环依赖）
ALTER TABLE `users`
  ADD CONSTRAINT `fk_users_current_llm_id`
  FOREIGN KEY (`current_llm_id`) REFERENCES `llms` (`id`)
  ON DELETE SET NULL ON UPDATE CASCADE;

-- 会话引用统计表（MemoryManager 使用）
CREATE TABLE IF NOT EXISTS `memory_references` (
  `id`          int           NOT NULL AUTO_INCREMENT COMMENT '主键',
  `turn_id`     varchar(64)   NOT NULL COMMENT '对话轮次ID（与ES turn_id对应）',
  `user_id`     varchar(50)   NOT NULL COMMENT '用户ID',
  `ref_count`   int           NOT NULL DEFAULT 0 COMMENT '被引用次数',
  `created_at`  timestamp     NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次记录时间',
  `last_ref_at` timestamp     NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近引用时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_turn_user` (`turn_id`, `user_id`),
  KEY `idx_user_id` (`user_id`),
  KEY `idx_turn_id` (`turn_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='会话引用统计表，用于长期会话管理（低引用+超期可清理）';

-- 创建agent表
CREATE TABLE IF NOT EXISTS `agents` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `agent_name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Agent 名称',
  `user_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '用户ID',
  `job` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '职责',
  `desc` json DEFAULT NULL COMMENT '信息描述，json字符串，含 background/tools 字段',
  `public` bigint DEFAULT '0' COMMENT '是否公有，1 共有；0 私有',
  `created_at` timestamp NULL DEFAULT NULL COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT NULL COMMENT '更新时间',
  `state` int DEFAULT '1' COMMENT '状态：0 未启用；1 启用；-1 已删除',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_name` (`agent_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Agent表';

-- Agent 技能记忆表（每个 Agent 最多 N 条技能，由配置控制）
CREATE TABLE IF NOT EXISTS `agent_skills` (
  `id`           int          NOT NULL AUTO_INCREMENT COMMENT '主键',
  `skill_id`     varchar(64)  NOT NULL COMMENT '技能唯一ID',
  `agent_name`   varchar(255) NOT NULL COMMENT '所属 Agent 名称',
  `description`  text         NOT NULL COMMENT '技能适用场景描述',
  `pattern`      text         NOT NULL COMMENT '成功工作模式（注入提示词）',
  `success_rate` float        NOT NULL DEFAULT 1.0 COMMENT '成功率 [0,1]',
  `usage_count`  int          NOT NULL DEFAULT 1   COMMENT '使用次数',
  `last_updated` timestamp    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_skill_id` (`skill_id`),
  KEY `idx_agent_name` (`agent_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Agent 技能记忆表';

-- 公有 Agent 评分表（每个用户每个 Agent 只能评一次，可覆盖）
CREATE TABLE IF NOT EXISTS `agent_ratings` (
  `id`         int        NOT NULL AUTO_INCREMENT COMMENT '主键',
  `agent_name` varchar(255) NOT NULL COMMENT '被评分的 Agent',
  `user_id`    varchar(50)  NOT NULL COMMENT '评分用户',
  `score`      tinyint      NOT NULL COMMENT '评分 1-5',
  `comment`    text         COMMENT '评论（可选）',
  `created_at` timestamp    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '评分时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_user` (`agent_name`, `user_id`),
  KEY `idx_agent_name` (`agent_name`),
  KEY `idx_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='公有 Agent 评分表';

-- 用户画像表（由 MemoryArchiverAgent 在记忆归档时自动维护）
CREATE TABLE IF NOT EXISTS `user_profiles` (
  `user_id`    varchar(50)  NOT NULL COMMENT '用户ID',
  `profile`    json         NOT NULL COMMENT '用户画像 JSON，含 preferences/personal_info/work_content',
  `updated_at` timestamp    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近更新时间',
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='用户画像表';

-- Agent 调用统计表（代码 Agent 和 DB Agent 均记录，不限用户）
CREATE TABLE IF NOT EXISTS `agent_call_stats` (
  `id`         bigint       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `agent_name` varchar(255) NOT NULL COMMENT 'Agent 名称',
  `source`     varchar(10)  NOT NULL DEFAULT 'db' COMMENT 'code|db',
  `called_at`  timestamp    NULL DEFAULT CURRENT_TIMESTAMP COMMENT '调用时间',
  PRIMARY KEY (`id`),
  KEY `idx_agent_name` (`agent_name`),
  KEY `idx_called_at` (`called_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Agent 调用统计表';

-- 工具定义（user / agent 来源工具持久化）
CREATE TABLE IF NOT EXISTS `tools` (
  `tool_id`          VARCHAR(32)   PRIMARY KEY,
  `name`             VARCHAR(64)   NOT NULL,
  `description`      TEXT,
  `source`           VARCHAR(10)   NOT NULL  COMMENT 'code|user|agent',
  `visibility`       VARCHAR(10)   NOT NULL  COMMENT 'public|private|exclusive',
  `exec_location`    VARCHAR(10)   NOT NULL  DEFAULT 'server' COMMENT 'server|client',
  `owner_user_id`    VARCHAR(36),
  `owner_agent`      VARCHAR(64),
  `dangerous_ops`    VARCHAR(200)            COMMENT 'JSON 数组',
  `parameters_schema` TEXT                  COMMENT 'JSON Schema',
  `code_source`      MEDIUMTEXT,
  `is_active`        TINYINT(1)    DEFAULT 1,
  `created_at`       DATETIME      DEFAULT NOW(),
  `updated_at`       DATETIME      DEFAULT NOW() ON UPDATE NOW(),
  INDEX idx_name         (`name`),
  INDEX idx_source       (`source`),
  INDEX idx_visibility   (`visibility`),
  INDEX idx_owner_user   (`owner_user_id`),
  INDEX idx_owner_agent  (`owner_agent`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='tools 定义表';

-- 调用统计
CREATE TABLE IF NOT EXISTS `tool_call_stats` (
  `id`               BIGINT AUTO_INCREMENT PRIMARY KEY,
  `tool_name`        VARCHAR(64)   NOT NULL,
  `caller_user_id`   VARCHAR(36),
  `caller_agent`     VARCHAR(64),
  `success`          TINYINT(1)    NOT NULL,
  `exec_ms`          INT,
  `error_info`       TEXT,
  `called_at`        DATETIME      DEFAULT NOW(),
  INDEX idx_tool_name  (`tool_name`),
  INDEX idx_called_at  (`called_at`),
  INDEX idx_caller     (`caller_user_id`, `tool_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='tools 调用统计';

-- 用户授权记录（session/project/always 级别持久化）
CREATE TABLE IF NOT EXISTS `tool_consent_records` (
  `id`               BIGINT AUTO_INCREMENT PRIMARY KEY,
  `tool_name`        VARCHAR(64)   NOT NULL,
  `operation`        VARCHAR(50)   NOT NULL,
  `user_id`          VARCHAR(36)   NOT NULL,
  `consent_level`    VARCHAR(20)   NOT NULL  COMMENT 'once|session|project|always',
  `session_id`       VARCHAR(64),
  `project_id`       VARCHAR(64),
  `granted_at`       DATETIME      DEFAULT NOW(),
  `expires_at`       DATETIME,
  INDEX idx_tool_user  (`tool_name`, `user_id`),
  INDEX idx_session    (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='tools 授权记录';

-- 危险操作类型用户级别开关（无记录 = 默认开启，需授权）
CREATE TABLE IF NOT EXISTS `dangerous_op_configs` (
  `id`          BIGINT AUTO_INCREMENT PRIMARY KEY,
  `user_id`     VARCHAR(36)  NOT NULL,
  `op_type`     VARCHAR(50)  NOT NULL,
  `is_enabled`  TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '1=开启需授权 0=关闭跳过授权',
  `created_at`  DATETIME     DEFAULT NOW(),
  UNIQUE KEY uq_user_op (`user_id`, `op_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='危险操作类型用户级别开关';
