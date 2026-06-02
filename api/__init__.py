"""FastAPI backend for the Sensgreen Sensor Simulator web UI.

This package is a thin presentation layer. It must not contain any
simulator, validator, or MQTT logic — it only orchestrates calls into
the underlying ``simulator`` package and renders templates.
"""
