#!/usr/bin/env python3
"""
Anthropic API代理配置管理
加载和解析YAML格式的配置文件，支持环境变量替换
"""

import os
import yaml
import re
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field


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
    timeout: int = 60
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
class SchemeRule:
    """转发方案中的单条规则"""
    model_pattern: str   # 支持 * 通配符
    provider: str        # provider 名称
    target_model: str    # 转发给 provider 的目标模型名


@dataclass
class SchemeConfig:
    """转发方案配置"""
    name: str
    description: str
    rules: List[SchemeRule] = field(default_factory=list)

    def match(self, model_name: str) -> Optional[SchemeRule]:
        """按顺序匹配规则，返回第一条命中的规则"""
        for rule in self.rules:
            pattern = rule.model_pattern.replace("*", ".*")
            if re.fullmatch(pattern, model_name):
                return rule
        return None


def _parse_scheme_rule(rule_str: str) -> SchemeRule:
    """解析单条规则字符串：'model_pattern -> provider_name:target_model'"""
    left, right = rule_str.split("->", 1)
    model_pattern = left.strip()
    provider, target_model = right.strip().split(":", 1)
    return SchemeRule(
        model_pattern=model_pattern,
        provider=provider.strip(),
        target_model=target_model.strip()
    )


class Config:
    """主配置类"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or "config.yaml"
        self.raw_config: Dict[str, Any] = {}
        self.server: Optional[ServerConfig] = None
        self.logging: Optional[LoggingConfig] = None
        self.providers: List[ProviderConfig] = []
        self.schemes: List[SchemeConfig] = []
        self.default_scheme: Optional[str] = None
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
                timeout=provider_data.get('timeout', 60),
                headers=provider_data.get('headers', {}),
                auth_type=provider_data.get('auth_type', 'bearer'),
                proxy_url=provider_data.get('proxy_url'),
                proxy_auth=provider_data.get('proxy_auth'),
                proxy_enabled=provider_data.get('proxy_enabled', False)
            )
            self.providers.append(provider)

        # 转发方案配置
        self.default_scheme = self.raw_config.get('default_scheme')
        schemes_data = self.raw_config.get('schemes', [])
        for scheme_data in schemes_data:
            scheme_rules = []
            for rule_str in scheme_data.get('rules', []):
                try:
                    scheme_rules.append(_parse_scheme_rule(rule_str))
                except Exception as e:
                    logging.warning(f"方案 '{scheme_data.get('name', '')}' 规则解析失败: {rule_str!r} -> {e}")
            scheme = SchemeConfig(
                name=scheme_data.get('name', ''),
                description=scheme_data.get('description', ''),
                rules=scheme_rules
            )
            self.schemes.append(scheme)

    def _set_defaults(self) -> None:
        """设置默认配置"""
        self.server = ServerConfig()
        self.logging = LoggingConfig()
        self.providers = []
        self.schemes = []
        self.default_scheme = None

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

            # 检查代理配置
            if provider.proxy_enabled:
                if not provider.proxy_url:
                    errors.append(f"提供商 {provider.name} 启用了代理但未设置proxy_url")
                elif not (provider.proxy_url.startswith("http://") or provider.proxy_url.startswith("https://")):
                    errors.append(f"提供商 {provider.name} 的proxy_url必须以http://或https://开头")

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
                "providers_with_proxy": providers_with_proxy
            },
            "providers": {
                "total": len(self.providers),
                "enabled": len(enabled_providers),
                "list": [p.name for p in enabled_providers]
            },
            "models": self.get_all_supported_models(),
        }

    def get_all_supported_models(self) -> List[str]:
        """获取所有支持的模型列表"""
        models = set()
        for provider in self.get_enabled_providers():
            models.update(provider.models)
        return sorted(list(models))

    def get_scheme_by_name(self, name: str) -> Optional[SchemeConfig]:
        """根据名称获取方案配置"""
        for scheme in self.schemes:
            if scheme.name == name:
                return scheme
        return None

    def get_default_scheme(self) -> Optional[SchemeConfig]:
        """获取默认方案（优先 default_scheme 字段，其次第一个方案）"""
        if self.default_scheme:
            scheme = self.get_scheme_by_name(self.default_scheme)
            if scheme:
                return scheme
        if self.schemes:
            return self.schemes[0]
        return None

    def get_provider_proxy_url(self, provider: ProviderConfig) -> Optional[str]:
        """获取provider的代理URL"""
        if provider.proxy_enabled and provider.proxy_url:
            return provider.get_proxy_url_with_auth()
        return None

    def update_provider_models(self, provider_name: str, models: List[str]) -> None:
        """
        将 models 列表同步到内存配置及 config.yaml 文件。

        只修改对应 provider 条目的 models 字段，其余内容保持不变。
        raw_config 中的环境变量占位符（${VAR}）会被还原为占位符形式写回，
        因为 raw_config 本身保存的就是原始字符串（未替换）。
        """
        # 1. 更新内存中的 ProviderConfig
        for provider in self.providers:
            if provider.name == provider_name:
                provider.models = list(models)
                break
        else:
            raise KeyError(f"provider '{provider_name}' 不存在")

        # 2. 更新 raw_config 中的对应条目
        for provider_data in self.raw_config.get("providers", []):
            if provider_data.get("name") == provider_name:
                provider_data["models"] = list(models)
                break

        # 3. 写回 config.yaml（保留原有格式，仅更新 providers 部分）
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                current_raw = yaml.safe_load(f.read())

            # 找到对应 provider，更新 models
            for pd in current_raw.get("providers", []):
                if pd.get("name") == provider_name:
                    pd["models"] = list(models)
                    break

            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(current_raw, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)

            logging.info(f"已更新 provider '{provider_name}' 的模型列表并写入 {self.config_path}")
        except Exception as e:
            logging.error(f"写回 config.yaml 失败: {e}")
            raise

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
        print(f"{summary['proxy']['providers_with_proxy']}个提供商使用代理")

        # 显示启用的提供商详情
        print("\n启用的提供商:")
        for provider in config.get_enabled_providers():
            proxy_info = ""
            if provider.proxy_enabled:
                proxy_info = f" [代理: {provider.proxy_url}]"
            print(f"  - {provider.name}: {provider.type} ({len(provider.models)}个模型){proxy_info}")

    except Exception as e:
        print(f"配置加载失败: {e}")
        sys.exit(1)