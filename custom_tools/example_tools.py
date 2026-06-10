"""Example custom tool"""
from langchain_core.tools import tool

@tool
def example_calculator(a: float, b: float, operation: str = "add") -> float:
    """Perform basic arithmetic operations.
    
    Args:
        a: First number
        b: Second number
        operation: Operation to perform (add, subtract, multiply, divide)
    
    Returns:
        Result of the operation
    """
    if operation == "add":
        return a + b
    elif operation == "subtract":
        return a - b
    elif operation == "multiply":
        return a * b
    elif operation == "divide":
        if b == 0:
            return "Error: Division by zero"
        return a / b
    else:
        return "Error: Unknown operation"
