# Packet Tracer Automation

This is a python library and a CLI tool that reuse the same connectivity as https://github.com/Mats2208/MCP-Packet-Tracer.git to 
build network configurations declaratively, using yaml file describing components, connections and configurations, and their relationships and define automated tests as pytest suites.

- components, aiming ad describing each component and attributes
The system provvides a CLI tool that reads a yaml file having three sections:
- connections, describing how components are interconnected.
- configurations, specifying the settings and parameters for each component.
Note: Optionally "components" can be coalesced into a single section including "configurations".

The CLI tool parses the yaml file and executes the necessary commands to build and configure the network accordingly.
The tool should ensure idempotency, meaning that the status of the network remain always consistent with the yaml file even if run multiple times.
The framework should also provide a set of pytest fixture to allow to easily execute commands and validate network configurations as a simple suite of pytest tests.


The system is using uv as a build and project management tool.
