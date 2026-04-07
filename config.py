#!/usr/bin/env python3
"""
Anthropic API代理配置管理
加载和解析YAML格式的配置文件，支持环境变量替换
"""

import os
import yaml
import re
import logging
from typing import Dict, List, Any, Optional, Union
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class ProviderConfig:
    """API提供商配置"""
    name: str
    enabled: bool = True
    type: str = "anthropic"
    base_url: str = ""
    api_key: str = ""
    api_version: Optional[str] = None
    models: List[str] = field(default_factory=list)
    weight: int = 10
    timeout: int = 60
    max_tokens_per_minute: int = 100000
    max_requests_per_minute: int = 1000
    priority: int = 1
    features: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    auth_type: str = "bearer"
    proxy_url: Optional[str] = None
    proxy_auth: Optional[str] = None
    proxy_enabled: bool = False

    def __post_init__(self):
        """初始化后处理"""
        # 确保base_url以/结尾
        if self.base_url and not self.base_url.endswith("/"):
            self.base_url = self.base_url + "/"

        # 验证代理配置
        if self.proxy_enabled and self.proxy_url:
            # 基本URL格式验证
            if not (self.proxy_url.startswith("http://") or self.proxy_url.startswith("https://")):
                raise ValueError(f"代理URL必须以http://或https://开头: {self.proxy_url}")

    def is_model_supported(self, model_name: str) -> bool:
        """检查是否支持特定模型"""
        for pattern in self.models:
            # 简单通配符匹配
            if "*" in pattern:
                pattern_re = pattern.replace("*", ".*")
                if re.match(pattern_re, model_name):
                    return True
            elif pattern == model_name:
                return True
        return False

    def get_auth_header(self) -> Dict[str, str]:
        """获取认证头信息"""
        if self.auth_type == "bearer":
            return {"Authorization": f"Bearer {self.api_key}"}
        elif self.auth_type == "api_key":
            return {"api-key": self.api_key}
        else:
            return {}

    def get_proxy_config(self) -> Optional[Dict[str, str]]:
        """获取代理配置，返回httpx可用的代理配置字典"""
        if not self.proxy_enabled or not self.proxy_url:
            return None

        # 基本代理配置
        proxy_config = {"http://": self.proxy_url, "https://": self.proxy_url}

        # 添加代理认证信息
        if self.proxy_auth:
            # 假设proxy_auth格式为 "username:password"
            proxy_config["http://"] = self.proxy_url
            proxy_config["https://"] = self.proxy_url

        return proxy_config

    def get_proxy_url_with_auth(self) -> Optional[str]:
        """获取带认证信息的完整代理URL"""
        if not self.proxy_enabled or not self.proxy_url:
            return None

        if self.proxy_auth:
            # 如果URL已经有认证信息，直接返回
            if "@" in self.proxy_url:
                return self.proxy_url
            # 否则添加认证信息
            # 格式: http://username:password@proxy.example.com:8080
            scheme, rest = self.proxy_url.split("://", 1)
            return f"{scheme}://{self.proxy_auth}@{rest}"

        return self.proxy_url


@dataclass
class ServerConfig:
    """服务器配置"""
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    timeout: int = 30
    max_requests: int = 1000


@dataclass
class LoggingConfig:
    """日志配置"""
    level: str = "INFO"
    file: Optional[str] = None
    max_size: str = "10MB"
    backup_count: int = 5
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    def get_log_level(self) -> int:
        """获取日志级别数值"""
        levels = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "CRITICAL": logging.CRITICAL
        }
        return levels.get(self.level.upper(), logging.INFO)


@dataclass
class RetryConfig:
    """重试配置"""
    max_attempts: int = 3
    backoff_factor: float = 1.5
    retryable_status_codes: List[int] = field(default_factory=lambda: [429, 500, 502, 503, 504])


@dataclass
class ProxyConfig:
    """HTTP代理配置（全局）"""
    enabled: bool = False
    url: Optional[str] = None
    auth: Optional[str] = None
    bypass_local: bool = True
    bypass_domains: List[str] = field(default_factory=lambda: ["localhost", "127.0.0.1", "*.internal"])

    def get_proxy_url_with_auth(self) -> Optional[str]:
        """获取带认证信息的完整代理URL"""
        if not self.enabled or not self.url:
            return None

        if self.auth:
            # 如果URL已经有认证信息，直接返回
            if "@" in self.url:
                return self.url
            # 否则添加认证信息
            # 格式: http://username:password@proxy.example.com:8080
            scheme, rest = self.url.split("://", 1)
            return f"{scheme}://{self.auth}@{rest}"

        return self.url

    def should_bypass(self, url: str) -> bool:
        """检查给定URL是否应该绕过代理"""
        if not self.enabled:
            return False

        # 检查是否本地地址
        if self.bypass_local:
            if "localhost" in url or "127.0.0.1" in url or "::1" in url:
                return True

        # 检查是否在绕过域名列表中
        import re
        for domain_pattern in self.bypass_domains:
            pattern_re = domain_pattern.replace("*", ".*").replace(".", r"\.")
            if re.search(pattern_re, url):
                return True

        return False


@dataclass
class LoadBalancingConfig:
    """负载均衡配置"""
    strategy: str = "round_robin"  # round_robin, weighted, least_connections
    health_check_interval: int = 30
    failover_enabled: bool = True


@dataclass
class RoutingRule:
    """路由规则"""
    rule_type: str  # model, parameter, time, default
    pattern: Optional[str] = None
    parameter: Optional[str] = None
    value: Optional[Any] = None
    providers: List[str] = field(default_factory=list)
    strategy: str = "round_robin"
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    weekdays: List[int] = field(default_factory=list)


class Config:
    """主配置类"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or "config.yaml"
        self.raw_config: Dict[str, Any] = {}
        self.server: Optional[ServerConfig] = None
        self.logging: Optional[LoggingConfig] = None
        self.retry: Optional[RetryConfig] = None
        self.proxy: Optional[ProxyConfig] = None
        self.load_balancing: Optional[LoadBalancingConfig] = None
        self.providers: List[ProviderConfig] = []
        self.routing_rules: List[RoutingRule] = []
        self._load_config()

    def _load_config(self) -> None:
        """加载和解析配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw_content = f.read()

            # 替换环境变量
            processed_content = self._replace_env_vars(raw_content)

            # 解析YAML
            self.raw_config = yaml.safe_load(processed_content)

            # 解析各配置部分
            self._parse_config()

            logging.info(f"配置文件加载成功: {self.config_path}")

        except FileNotFoundError:
            logging.warning(f"配置文件不存在: {self.config_path}, 使用默认配置")
            self._set_defaults()
        except yaml.YAMLError as e:
            raise ValueError(f"配置文件解析失败: {e}")
        except Exception as e:
            raise ValueError(f"配置加载失败: {e}")

    def _replace_env_vars(self, content: str) -> str:
        """替换环境变量占位符 ${VAR_NAME}"""
        def replace_match(match):
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))

        # 匹配 ${VAR_NAME} 格式
        pattern = r'\$\{([^}]+)\}'
        return re.sub(pattern, replace_match, content)

    def _parse_config(self) -> None:
        """解析配置数据"""
        # 服务器配置
        server_data = self.raw_config.get('server', {})
        self.server = ServerConfig(
            host=server_data.get('host', '0.0.0.0'),
            port=server_data.get('port', 8000),
            workers=server_data.get('workers', 1),
            timeout=server_data.get('timeout', 30),
            max_requests=server_data.get('max_requests', 1000)
        )

        # 日志配置
        logging_data = self.raw_config.get('logging', {})
        self.logging = LoggingConfig(
            level=logging_data.get('level', 'INFO'),
            file=logging_data.get('file'),
            max_size=logging_data.get('max_size', '10MB'),
            backup_count=logging_data.get('backup_count', 5),
            format=logging_data.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        )

        # 重试配置
        retry_data = self.raw_config.get('retry', {})
        self.retry = RetryConfig(
            max_attempts=retry_data.get('max_attempts', 3),
            backoff_factor=retry_data.get('backoff_factor', 1.5),
            retryable_status_codes=retry_data.get('retryable_status_codes', [429, 500, 502, 503, 504])
        )

        # HTTP代理配置（全局）
        proxy_data = self.raw_config.get('proxy', {})
        self.proxy = ProxyConfig(
            enabled=proxy_data.get('enabled', False),
            url=proxy_data.get('url'),
            auth=proxy_data.get('auth'),
            bypass_local=proxy_data.get('bypass_local', True),
            bypass_domains=proxy_data.get('bypass_domains', ["localhost", "127.0.0.1", "*.internal"])
        )

        # 负载均衡配置
        lb_data = self.raw_config.get('load_balancing', {})
        self.load_balancing = LoadBalancingConfig(
            strategy=lb_data.get('strategy', 'round_robin'),
            health_check_interval=lb_data.get('health_check_interval', 30),
            failover_enabled=lb_data.get('failover_enabled', True)
        )

        # 提供商配置
        providers_data = self.raw_config.get('providers', [])
        for provider_data in providers_data:
            provider = ProviderConfig(
                name=provider_data.get('name', ''),
                enabled=provider_data.get('enabled', True),
                type=provider_data.get('type', 'anthropic'),
                base_url=provider_data.get('base_url', ''),
                api_key=provider_data.get('api_key', ''),
                api_version=provider_data.get('api_version'),
                models=provider_data.get('models', []),
                weight=provider_data.get('weight', 10),
                timeout=provider_data.get('timeout', 60),
                max_tokens_per_minute=provider_data.get('max_tokens_per_minute', 100000),
                max_requests_per_minute=provider_data.get('max_requests_per_minute', 1000),
                priority=provider_data.get('priority', 1),
                features=provider_data.get('features', []),
                headers=provider_data.get('headers', {}),
                auth_type=provider_data.get('auth_type', 'bearer'),
                proxy_url=provider_data.get('proxy_url'),
                proxy_auth=provider_data.get('proxy_auth'),
                proxy_enabled=provider_data.get('proxy_enabled', False)
            )
            self.providers.append(provider)

        # 路由规则
        rules_data = self.raw_config.get('routing_rules', [])
        for rule_data in rules_data:
            rule = RoutingRule(
                rule_type=rule_data.get('rule_type', ''),
                pattern=rule_data.get('pattern'),
                parameter=rule_data.get('parameter'),
                value=rule_data.get('value'),
                providers=rule_data.get('providers', []),
                strategy=rule_data.get('strategy', 'round_robin'),
                start_time=rule_data.get('start_time'),
                end_time=rule_data.get('end_time'),
                weekdays=rule_data.get('weekdays', [])
            )
            self.routing_rules.append(rule)

    def _set_defaults(self) -> None:
        """设置默认配置"""
        self.server = ServerConfig()
        self.logging = LoggingConfig()
        self.retry = RetryConfig()
        self.proxy = ProxyConfig()
        self.load_balancing = LoadBalancingConfig()
        self.providers = []
        self.routing_rules = []

    def get_enabled_providers(self) -> List[ProviderConfig]:
        """获取启用的提供商列表"""
        return [p for p in self.providers if p.enabled]

    def get_provider_by_name(self, name: str) -> Optional[ProviderConfig]:
        """根据名称获取提供商配置"""
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None

    def get_providers_for_model(self, model_name: str) -> List[ProviderConfig]:
        """获取支持指定模型的提供商列表"""
        result = []
        enabled_providers = self.get_enabled_providers()

        for provider in enabled_providers:
            if provider.is_model_supported(model_name):
                result.append(provider)

        return result

    def validate(self) -> List[str]:
        """验证配置，返回错误信息列表"""
        errors = []

        # 检查启用的提供商
        enabled_providers = self.get_enabled_providers()
        if not enabled_providers:
            errors.append("至少需要启用一个API提供商")

        # 检查每个启用的提供商
        for provider in enabled_providers:
            if not provider.name:
                errors.append(f"提供商名称不能为空")
            if not provider.base_url:
                errors.append(f"提供商 {provider.name} 的base_url不能为空")
            if not provider.api_key:
                errors.append(f"提供商 {provider.name} 的api_key不能为空")
            if not provider.models:
                errors.append(f"提供商 {provider.name} 的models列表不能为空")
            if provider.weight <= 0:
                errors.append(f"提供商 {provider.name} 的weight必须大于0")

            # 检查代理配置
            if provider.proxy_enabled:
                if not provider.proxy_url:
                    errors.append(f"提供商 {provider.name} 启用了代理但未设置proxy_url")
                elif not (provider.proxy_url.startswith("http://") or provider.proxy_url.startswith("https://")):
                    errors.append(f"提供商 {provider.name} 的proxy_url必须以http://或https://开头")

        # 检查路由规则
        for rule in self.routing_rules:
            if not rule.rule_type:
                errors.append("路由规则的rule_type不能为空")
            if rule.rule_type == "model" and not rule.pattern:
                errors.append("模型路由规则必须指定pattern")
            if not rule.providers:
                errors.append(f"路由规则 {rule.rule_type} 的providers列表不能为空")

            # 检查引用的提供商是否存在
            for provider_name in rule.providers:
                if not self.get_provider_by_name(provider_name):
                    errors.append(f"路由规则引用了不存在的提供商: {provider_name}")

        # 检查全局代理配置
        if self.proxy and self.proxy.enabled:
            if not self.proxy.url:
                errors.append("启用了全局代理但未设置url")
            elif not (self.proxy.url.startswith("http://") or self.proxy.url.startswith("https://")):
                errors.append("全局代理的url必须以http://或https://开头")

        return errors

    def get_config_summary(self) -> Dict[str, Any]:
        """获取配置摘要（用于显示）"""
        enabled_providers = self.get_enabled_providers()

        # 统计使用代理的提供商数量
        providers_with_proxy = sum(1 for p in enabled_providers if p.proxy_enabled)

        return {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "workers": self.server.workers
            },
            "proxy": {
                "enabled": self.proxy.enabled if self.proxy else False,
                "global_enabled": self.proxy.enabled if self.proxy else False,
                "providers_with_proxy": providers_with_proxy
            },
            "providers": {
                "total": len(self.providers),
                "enabled": len(enabled_providers),
                "list": [p.name for p in enabled_providers]
            },
            "models": self.get_all_supported_models(),
            "routing_rules": len(self.routing_rules),
            "load_balancing": self.load_balancing.strategy
        }

    def get_all_supported_models(self) -> List[str]:
        """获取所有支持的模型列表"""
        models = set()
        for provider in self.get_enabled_providers():
            models.update(provider.models)
        return sorted(list(models))

    def get_provider_proxy_url(self, provider: ProviderConfig) -> Optional[str]:
        """
        获取provider的最终代理URL
        优先级：provider特定代理 > 全局代理
        """
        # 首先检查provider是否启用了自己的代理
        if provider.proxy_enabled and provider.proxy_url:
            return provider.get_proxy_url_with_auth()

        # 然后检查全局代理
        if self.proxy and self.proxy.enabled and self.proxy.url:
            # 检查是否应该绕过代理
            if self.proxy.should_bypass(provider.base_url):
                return None
            return self.proxy.get_proxy_url_with_auth()

        return None

    def save(self, path: Optional[str] = None) -> None:
        """保存配置到文件"""
        save_path = path or self.config_path

        # 将配置对象转换回字典
        config_dict = self._to_dict()

        with open(save_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        logging.info(f"配置文件已保存: {save_path}")

    def _to_dict(self) -> Dict[str, Any]:
        """将配置对象转换为字典"""
        # 这里简化处理，直接返回原始配置
        # 在实际实现中，需要将各配置对象转换回字典
        return self.raw_config.copy()


def load_config(config_path: Optional[str] = None) -> Config:
    """
    加载配置的便捷函数

    Args:
        config_path: 配置文件路径，默认为config.yaml

    Returns:
        Config: 配置对象
    """
    return Config(config_path)


if __name__ == "__main__":
    # 测试配置加载
    import sys

    # 设置环境变量用于测试
    os.environ["ANTHROPIC_API_KEY"] = "test-key-from-env"

    try:
        config = load_config("config.yaml")

        # 验证配置
        errors = config.validate()
        if errors:
            print("配置错误:")
            for error in errors:
                print(f"  - {error}")
            sys.exit(1)

        # 显示配置摘要
        summary = config.get_config_summary()
        print("配置加载成功!")
        print(f"服务器: {summary['server']['host']}:{summary['server']['port']}")
        print(f"提供商: {summary['providers']['enabled']}/{summary['providers']['total']} 个已启用")
        print(f"支持模型: {', '.join(summary['models'][:5])}... (共{len(summary['models'])}个)")
        print(f"路由规则: {summary['routing_rules']} 条")
        print(f"负载均衡策略: {summary['load_balancing']}")
        print(f"代理配置: 全局代理{'已启用' if summary['proxy']['global_enabled'] else '未启用'}, "
              f"{summary['proxy']['providers_with_proxy']}个提供商使用代理")

        # 显示启用的提供商详情
        print("\n启用的提供商:")
        for provider in config.get_enabled_providers():
            proxy_info = ""
            if provider.proxy_enabled:
                proxy_info = f" [代理: {provider.proxy_url}]"
            print(f"  - {provider.name}: {provider.type} ({len(provider.models)}个模型){proxy_info}")

        # 显示代理配置详情
        if config.proxy and config.proxy.enabled:
            print("\n全局代理配置:")
            print(f"  代理URL: {config.proxy.url}")
            print(f"  绕过本地地址: {config.proxy.bypass_local}")
            print(f"  绕过域名: {', '.join(config.proxy.bypass_domains[:3])}...")

    except Exception as e:
        print(f"配置加载失败: {e}")
        sys.exit(1)