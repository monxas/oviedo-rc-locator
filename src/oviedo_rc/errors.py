"""Excepciones del paquete."""


class OviedoError(Exception):
    """Error genérico del pipeline (red persistente, listado roto, etc.)."""


class RCError(ValueError, OviedoError):
    """Error de validación o resolución de una RC."""
