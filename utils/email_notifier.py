"""
邮件通知工具模块

用于发送告警邮件通知
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import logging


class EmailNotifier:
    """邮件通知类"""

    def __init__(self, config, logger=None):
        """
        初始化邮件通知器

        Args:
            config (dict): 邮件配置字典，包含 smtp_server, smtp_port, use_tls,
                          sender_email, sender_password, recipient_emails
            logger (logging.Logger, optional): 日志记录器
        """
        self.smtp_server = config.get('smtp_server')
        self.smtp_port = config.get('smtp_port', 587)
        self.use_tls = config.get('use_tls', True)
        self.sender_email = config.get('sender_email')
        self.sender_password = config.get('sender_password')
        self.recipient_emails = config.get('recipient_emails', [])
        self.logger = logger or logging.getLogger(__name__)

        # 验证配置
        if not all([self.smtp_server, self.sender_email, self.sender_password, self.recipient_emails]):
            self.logger.warning("邮件配置不完整，邮件通知功能将无法使用")

    def send_alert(self, subject, server_ip, cpu_temps, fan_speeds, failed_attempts, threshold):
        """
        发送告警邮件

        Args:
            subject (str): 邮件主题
            server_ip (str): 服务器 IP
            cpu_temps (list): CPU 温度列表
            fan_speeds (list): 风扇转速列表
            failed_attempts (int): 连续失败次数
            threshold (int): 风扇转速阈值

        Returns:
            bool: 发送成功返回 True，失败返回 False
        """
        try:
            # 构建邮件内容
            message = MIMEMultipart()
            message['From'] = self.sender_email
            message['To'] = ', '.join(self.recipient_emails)
            message['Subject'] = subject

            # 邮件正文
            max_temp = max(cpu_temps) if cpu_temps else 0
            max_fan_speed = max(fan_speeds) if fan_speeds else 0
            cpu_temps_str = ', '.join([f"{temp}°C" for temp in cpu_temps])
            fan_speeds_str = ', '.join([f"{speed} RPM" for speed in fan_speeds])

            body = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .alert {{ background-color: #fff3cd; border: 1px solid #ffc107; padding: 15px; border-radius: 5px; }}
        .critical {{ background-color: #f8d7da; border-color: #dc3545; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 15px; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f2f2f2; }}
        .warning {{ color: #856404; font-weight: bold; }}
        .critical-text {{ color: #721c24; font-weight: bold; }}
    </style>
</head>
<body>
    <div class="alert critical">
        <h2 class="critical-text">⚠️ IPMI 风扇控制器告警</h2>
        <p><strong>告警时间:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
        <p><strong>服务器 IP:</strong> {server_ip}</p>
        <p class="critical-text"><strong>告警原因:</strong> 风扇转速调节失败，连续 {failed_attempts} 次调节后风扇转速仍超过阈值 {threshold} RPM</p>
    </div>

    <h3>当前状态</h3>
    <table>
        <tr>
            <th>项目</th>
            <th>详细信息</th>
        </tr>
        <tr>
            <td>CPU 温度</td>
            <td>{cpu_temps_str}<br><strong>最高温度:</strong> {max_temp}°C</td>
        </tr>
        <tr>
            <td>风扇转速</td>
            <td>{fan_speeds_str}<br><strong class="critical-text">最高转速:</strong> {max_fan_speed} RPM</td>
        </tr>
        <tr>
            <td>失败次数</td>
            <td>{failed_attempts} 次</td>
        </tr>
        <tr>
            <td>转速阈值</td>
            <td>{threshold} RPM</td>
        </tr>
    </table>

    <h3>可能原因</h3>
    <ul>
        <li>IPMI 手动模式未正确设置，服务器处于自动温度控制模式</li>
        <li>Dell 第三方 PCIe 卡散热响应策略未禁用</li>
        <li>IPMI 命令执行失败</li>
        <li>服务器温度过高，自动保护机制接管风扇控制</li>
        <li>硬件故障或风扇控制器异常</li>
    </ul>

    <h3>建议操作</h3>
    <ol>
        <li>检查服务器 IPMI 连接是否正常</li>
        <li>检查服务器温度是否异常过高</li>
        <li>检查日志文件获取详细错误信息</li>
        <li>考虑手动介入，检查服务器硬件状态</li>
        <li>如持续告警，建议停止自动控制，让服务器自动管理风扇</li>
    </ol>

    <p style="margin-top: 20px; color: #666; font-size: 12px;">
        此邮件由 IPMI 风扇控制器自动发送，请勿直接回复。
    </p>
</body>
</html>
"""

            message.attach(MIMEText(body, 'html'))

            # 发送邮件
            if self.use_tls:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port)
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port)

            server.login(self.sender_email, self.sender_password)
            server.send_message(message)
            server.quit()

            self.logger.info(f"告警邮件已发送至: {', '.join(self.recipient_emails)}")
            return True

        except Exception as e:
            self.logger.error(f"发送告警邮件失败: {str(e)}")
            return False
