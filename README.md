# SelfHeal - AI Self-Healing Microservices Platform

An AI-assisted control loop for microservices: it watches service health signals, detects
anomalies, diagnoses the likely root cause (rules + an LLM), and applies **guarded, verified**
remediations such as restart, scale-out, rollback or circuit-breaking - and rolls back or
escalates to a human when a fix does not work.

Works fully offline with a deterministic mock LLM provider; OpenAI, Groq and Ollama are
supported through an OpenAI-compatible client.

> Work in progress - full documentation coming with v1.0.0.
