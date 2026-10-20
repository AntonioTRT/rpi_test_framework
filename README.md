# Test Framework

## Running a Test

Select a test through its YAML configuration in `tests/<test-name>/config.yaml`, then run the framework from this folder:

```bash
python main.py
```

## Test Modules

A test module is a Python file that performs one test. Each test has its own folder and YAML configuration.

## Loop Tests

Loop tests repeat a check over time or for a configured number of iterations.

## Folders

- `tests/` contains the test modules and their configurations.
- `logs/` contains test output.
- The framework folder contains the runner and shared support files.
