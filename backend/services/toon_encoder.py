"""
TOON (Token-Oriented Object Notation) Encoder for Python

Converts Python data structures to TOON format for efficient LLM communication.
TOON reduces token count by 30-60% compared to JSON for tabular data.

Format:
    array_name[length]{field1,field2,field3}:
      value1,value2,value3
      value4,value5,value6

Reference: https://github.com/toon-format/toon
"""

from typing import Any, Dict, List, Union
import json


def _flatten_dict(obj: Dict[str, Any], parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    """
    Flatten nested dictionary using dot notation.

    Example:
        {"technical": {"score": 0.5, "signal": "BUY"}}
        -> {"technical.score": 0.5, "technical.signal": "BUY"}
    """
    items = []
    for k, v in obj.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(_flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def _escape_value(value: Any) -> str:
    """
    Escape value for TOON format.
    - None -> empty string
    - String with comma/newline -> JSON encode
    - Other -> str()
    """
    if value is None:
        return ""
    if isinstance(value, str):
        # If string contains comma or newline, use JSON encoding
        if "," in value or "\n" in value or '"' in value:
            return json.dumps(value)
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    # Complex types -> JSON
    return json.dumps(value)


def _encode_array(name: str, items: List[Dict[str, Any]], max_fields: int = 50) -> str:
    """
    Encode array of uniform objects to TOON tabular format.

    Args:
        name: Array name
        items: List of dictionaries with same structure
        max_fields: Maximum fields to include (avoid too wide tables)

    Returns:
        TOON formatted string
    """
    if not items:
        return f"{name}[0]:\n"

    # Flatten all items to get all possible fields
    flattened_items = [_flatten_dict(item) for item in items]

    # Get all unique fields (preserve order from first item)
    all_fields = []
    seen = set()
    for item in flattened_items:
        for field in item.keys():
            if field not in seen:
                all_fields.append(field)
                seen.add(field)

    # Limit fields to avoid extremely wide tables
    if len(all_fields) > max_fields:
        # Prioritize important fields (heuristic: shorter names often more important)
        all_fields = sorted(all_fields, key=lambda x: (len(x), x))[:max_fields]

    # Build header: name[length]{field1,field2,...}:
    header = f"{name}[{len(items)}]{{{','.join(all_fields)}}}:\n"

    # Build rows
    rows = []
    for item in flattened_items:
        row_values = [_escape_value(item.get(field)) for field in all_fields]
        rows.append("  " + ",".join(row_values))

    return header + "\n".join(rows)


def _encode_object(name: str, obj: Dict[str, Any]) -> str:
    """
    Encode single object to TOON format.

    Format:
        name{field1,field2,...}:
          value1,value2,...
    """
    if not obj:
        return f"{name}{{}}:\n"

    flattened = _flatten_dict(obj)
    fields = list(flattened.keys())
    values = [_escape_value(flattened[f]) for f in fields]

    header = f"{name}{{{','.join(fields)}}}:\n"
    row = "  " + ",".join(values)

    return header + row


def encode(data: Union[Dict, List], root_name: str = "data") -> str:
    """
    Encode Python data structure to TOON format.

    Args:
        data: Dictionary or list to encode
        root_name: Name for root element

    Returns:
        TOON formatted string

    Example:
        >>> data = {
        ...     "symbols": [
        ...         {"symbol": "BTC", "price": 100, "signal": "BUY"},
        ...         {"symbol": "ETH", "price": 50, "signal": "HOLD"}
        ...     ],
        ...     "portfolio": {"cash": 1000, "positions": 2}
        ... }
        >>> print(encode(data))
        symbols[2]{symbol,price,signal}:
          BTC,100,BUY
          ETH,50,HOLD
        portfolio{cash,positions}:
          1000,2
    """
    if isinstance(data, list):
        return _encode_array(root_name, data)

    if not isinstance(data, dict):
        raise ValueError(f"Cannot encode type {type(data)} - must be dict or list")

    # Encode each top-level key
    parts = []

    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            # Array of objects -> tabular format
            parts.append(_encode_array(key, value))
        elif isinstance(value, dict):
            # Single object
            parts.append(_encode_object(key, value))
        else:
            # Primitive value -> simple key: value
            parts.append(f"{key}: {_escape_value(value)}")

    return "\n\n".join(parts)


def estimate_token_savings(json_str: str, toon_str: str) -> Dict[str, Any]:
    """
    Estimate token savings from JSON to TOON.

    Uses simple character count as proxy for tokens (rough estimate).
    Real tokenization would vary by model.
    """
    json_chars = len(json_str)
    toon_chars = len(toon_str)

    # Rough estimate: ~4 chars per token on average
    json_tokens = json_chars / 4
    toon_tokens = toon_chars / 4

    savings_pct = ((json_tokens - toon_tokens) / json_tokens * 100) if json_tokens > 0 else 0

    return {
        "json_chars": json_chars,
        "toon_chars": toon_chars,
        "json_tokens_estimate": int(json_tokens),
        "toon_tokens_estimate": int(toon_tokens),
        "savings_pct": round(savings_pct, 1),
        "savings_tokens": int(json_tokens - toon_tokens)
    }
