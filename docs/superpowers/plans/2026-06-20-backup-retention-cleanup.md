# Backup Retention Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 ERP 备份建立“只保留最近 5 个常规备份”的自动策略，并清理当前多余常规备份，但绝不删除主库、沙盒、副本、迁移报告和受保护迁移备份。

**Architecture:** 在共享后端模块中实现备份分类与清理逻辑，CLI 脚本只做参数解析和报告输出；所有成功创建常规备份的入口统一复用该共享逻辑，在备份成功后再触发受保护的自动清理。

**Tech Stack:** Python 3.12, pathlib, sqlite3, pytest

---
