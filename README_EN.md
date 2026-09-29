[中文文档](./README.md)

# Python IPMI Fan Controller

A Python script to monitor server CPU temperature via IPMI and adjust fan speeds according to predefined temperature ranges.

## Cross-Platform Support (Windows & Linux)!

## Compatible Servers

The following models have been tested and are confirmed to work. More models are pending testing; contributions are welcome!

| Brand | Model | Compatible | Type Name |
|:-----:|:-----:|:----------:|:---------:|
| Dell  | 730XD |     Y      | `dell730` |
| Dell  | 730   |     Y      | `dell730` |

## How to Use

> On Linux, `ipmitool` must be installed.
> 
> For Debian-based systems, install it with `apt install -y ipmitool`.
> For Red Hat-based systems, use `yum install -y ipmitool`.

1.  **Clone the project**

    ```
    git clone https://github.com/dongyu6/python-ipmitool.git
    ```

2.  **Navigate to the project directory**

    ```
    cd python-ipmitool
    ```

3.  **Install dependencies**

    ```
    pip install -r requirements.txt
    ```

4.  **Copy the template file** `fan_settings.yaml.template` to `fan_settings.yaml`.

    ```bash
    # Linux/Mac
    cp fan_settings.yaml.template fan_settings.yaml

    # Windows
    copy fan_settings.yaml.template fan_settings.yaml
    ```

5.  **Edit the newly created `fan_settings.yaml`** file. The meaning of each field is as follows. You need to configure the IP addresses and fan speeds yourself.

    > Note: Only IP addresses are supported, not domain names.

    ```yaml
    # IPMI Fan Controller Configuration

    # true for automatic fan control, false for manual
    auto: true

    # The interval in seconds for checking temperature and adjusting fan speed
    interval: 60

    # Number of days to retain log files
    log_backup_count: 30

    # Path to the ipmitool executable on Windows
    windows_ipmi_tool_path: ".\\ipmitool\\ipmitool.exe"

    # List of servers to manage
    servers:
      - type: dell730                    # Server type
        ip: "192.168.71.90"              # Server IP address. Set to "local" if running on the target machine
        user: root                       # IPMI username
        password: "123123"               # IPMI password
        temperature_ranges:              # List of temperature ranges and corresponding fan speeds
          - min_temp: 0                  # Minimum temperature of the range (inclusive)
            max_temp: 60                 # Maximum temperature of the range (inclusive)
            fan_speeds: [20, 20, 20, 20, 20, 20]  # List of fan speeds in percent
          - min_temp: 61
            max_temp: 80
            fan_speeds: [25, 25, 25, 25, 25, 25]
    ```


6.  **Run the Project**

    The project offers two execution modes:

    ## Mode 1: Loop Control Mode (Recommended for Long-term Operation)

    The program continuously monitors temperature and adjusts fan speeds. Suitable for running as a background service.

    **Foreground Execution (for debugging)**
    ```bash
    # Windows
    python fancontroller.py

    # Linux
    python3 fancontroller.py
    ```

    **Background Execution**
    ```bash
    # Windows
    start /b python fancontroller.py

    # Linux
    nohup python3 fancontroller.py &
    ```

    ## Mode 2: One-Shot Execution Mode (Recommended for External Scheduling)

    Executes once and exits after temperature detection and fan adjustment. Suitable for being called by external scheduling tools like cron, systemd timer, etc.

    **Direct Execution**
    ```bash
    # Windows
    python fancontroller_once.py

    # Linux
    python3 fancontroller_once.py
    ```

    **Using cron for Scheduled Execution (Linux)**
    ```bash
    # Edit crontab
    crontab -e

    # Execute every 10 minutes
    */10 * * * * /usr/bin/python3 /path/to/python-ipmitool/fancontroller_once.py
    ```

    **Using Windows Task Scheduler**
    ```powershell
    # Create a task that runs every 10 minutes
    schtasks /create /tn "IPMI Fan Controller" /tr "python C:\path\to\python-ipmitool\fancontroller_once.py" /sc minute /mo 10
    ```

### Setup as a systemd Service (Linux Recommended)

For reliable operation on a Linux server, setting up a systemd service is highly recommended. This provides features like auto-start on boot and process supervision.

1.  **Create the Service File**

    Use a text editor (like `nano` or `vim`) to create a new service file:
    ```
    sudo nano /etc/systemd/system/fancontroller.service
    ```

2.  **Paste the Service Configuration**

    Paste the following content into the file. **Note:** You must replace the paths for `User`, `WorkingDirectory`, and `ExecStart` with the actual paths on your server.

    ```ini
    [Unit]
    Description=Python IPMI Fan Controller
    After=network.target

    [Service]
    Type=simple
    # If you use a non-root user, ensure they have permissions for ipmitool.
    User=root
    # Absolute path to the project directory.
    WorkingDirectory=/path/to/python-ipmitool
    # Absolute path to the Python interpreter and the script.
    ExecStart=/usr/bin/python3 /path/to/python-ipmitool/fancontroller.py
    Restart=always
    RestartSec=3

    [Install]
    WantedBy=multi-user.target
    ```

3.  **Reload and Enable the Service**

    Run the following commands to reload the systemd configuration, start the service, and enable it to start on boot.

    ```bash
    # Reload the systemd configuration
    sudo systemctl daemon-reload

    # Start the service
    sudo systemctl start fancontroller.service

    # Check the service status to ensure there are no errors
    sudo systemctl status fancontroller.service

    # Enable the service to start automatically on boot
    sudo systemctl enable fancontroller.service
    ```

4.  **View Logs**

    Once configured as a service, all output (including errors) can be viewed with `journalctl`:
    ```bash
    journalctl -u fancontroller.service -f
    ```

## Contributing and Feedback

Contributions via Issues and Pull Requests are welcome to help improve the project. If you have any questions or suggestions, please provide feedback through GitHub Issues.

## License

This project is licensed under the MIT License. See the LICENSE file for details.

## Acknowledgements

- [perryclements/r410-fancontroller: Python fan controller for Dell R410 server (GitHub.com)](https://github.com/perryclements/r410-fancontroller)
- [ipmitool/ipmitool: An open-source tool for controlling IPMI-enabled systems (GitHub.com)](https://github.com/ipmitool/ipmitool)
