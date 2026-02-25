# UFactory Studio Docker Setup (Simulator)

Run UFactory Studio in Docker to simulate the arm without a physical connection.

## Quick Start

```bash
# Pull image
docker pull danielwang123321/uf-ubuntu-docker

# Create container (all ports: web + SDK)
docker run -it --name uf_software \
  -p 18333:18333 \
  -p 502:502 -p 503:503 -p 504:504 \
  -p 30000:30000 -p 30001:30001 -p 30002:30002 -p 30003:30003 \
  danielwang123321/uf-ubuntu-docker

# Start the 850 firmware inside the container
/xarm_scripts/xarm_start.sh 6 12
```

Open **http://localhost:18333** for the web simulation UI.

## Robot Model Parameters

| Model   | Parameter |
|---------|-----------|
| xArm 5  | `5 5`     |
| xArm 6  | `6 6`     |
| xArm 7  | `7 7`     |
| Lite 6  | `6 9`     |
| **850**  | **`6 12`** |

## Connecting via Python SDK

Use `127.0.0.1` as the IP when connecting from the host:

```python
from xarm.wrapper import XArmAPI
arm = XArmAPI('127.0.0.1')
```

> When running Blockly-exported Python code externally, add `check_joint_limit=False` to the XArmAPI instantiation.

## Port Reference

| Port       | Purpose                        |
|------------|--------------------------------|
| 18333      | Web simulation UI              |
| 502–504    | Modbus protocol                |
| 30000–30003| SDK / firmware communication   |

## Useful Docker Commands

```bash
docker ps                              # Running containers
docker stop uf_software                # Stop
docker start uf_software               # Restart
docker exec -it uf_software /bin/bash  # Shell into container
```

## Notes

- "Unable to get robot SN" warning is normal in simulation — click Close
- Tested on Windows 11 x86-64 and Ubuntu 24.04 x86-64
