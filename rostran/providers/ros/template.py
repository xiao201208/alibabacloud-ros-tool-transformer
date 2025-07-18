import os
import textwrap
import uuid
from pathlib import Path
from typing import Any, Optional, List

import typer
import json

from Tea.exceptions import TeaException
from alibabacloud_credentials.exceptions import CredentialException
from alibabacloud_tea_util.models import RuntimeOptions
from collections import defaultdict
from libterraform import TerraformCommand
from libterraform.exceptions import TerraformCommandError
from ruamel import yaml
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_ros20190910.client import Client
from alibabacloud_credentials.client import Client as CredClient
from alibabacloud_ros20190910 import models

from rostran.core.exceptions import (
    TemplateFormatNotSupport, InvalidTemplateWorkspace, InvalidTargetPath,
    InvalidTemplate
)
from rostran.core.rule_manager import RuleManager, RuleClassifier, ResourceRule
from rostran.core.template import Template
from rostran.core.format import FileFormat
from rostran.providers.ros import functions
from rostran.providers.terraform import template_blocks as tf
from tools.utils import camel_to_snake


class WrapTerraformTemplate(Template):
    @classmethod
    def initialize(
        cls,
        path: str,
        format: FileFormat = FileFormat.Terraform,
    ):

        if format == FileFormat.Json:
            with open(path) as f:
                source = json.load(f)
        elif format == FileFormat.Yaml:
            with open(path) as f:
                source = yaml.load(f)
        else:
            raise TemplateFormatNotSupport(path=path, format=format)

        return cls(source=source)

    def transform(self, target_path=None):
        typer.secho(f"Transforming ROS template to terraform template...")

        workspace = self.source.get("Workspace")
        if not isinstance(workspace, dict):
            raise InvalidTemplateWorkspace(
                reason=f"The type of data ({workspace}) should be dict"
            )

        for file_name, file_content in workspace.items():
            file_path = target_path + "/" + file_name
            dir_path = "/".join(file_path.split("/")[:-1])
            if not os.path.exists(dir_path):
                os.makedirs(dir_path)
            with open(file_path, "w") as f:
                f.write(file_content)
        typer.secho(
            f"Transform ROS template to terraform template successful.",
            fg="green",
        )


class ResourcesInProperty:

    def __init__(self, resource_rule: ResourceRule, resources: dict, from_res_name: str):
        self.resource_rule = resource_rule
        self.resources = resources
        self.from_res_name = from_res_name


class ROS2TerraformTemplate(Template):
    (
        ROS_TEMPLATE_FORMAT_VERSION,
        TRANSFORM,
        DESCRIPTION,
        CONDITIONS,
        MAPPINGS,
        PARAMETERS,
        RESOURCES,
        OUTPUTS,
        RULES,
        METADATA,
        WORKSPACE,
        LOCALS
    ) = (
        "ROSTemplateFormatVersion",
        "Transform",
        "Description",
        "Conditions",
        "Mappings",
        "Parameters",
        "Resources",
        "Outputs",
        "Rules",
        "Metadata",
        "Workspace",
        "Locals"
    )

    TF_INVALID_VARIABLE_KEYS = (
        "source",
        "version",
        "providers",
        "count",
        "for_each",
        "lifecycle",
        "depends_on",
        "locals"
    )

    def __init__(self, source: dict, rule_manager: RuleManager):
        super().__init__(source)
        self.rule_manager = rule_manager
        self.parameters = source.get(self.PARAMETERS) or {}
        self.resources = source.get(self.RESOURCES) or {}
        self.outputs = source.get(self.OUTPUTS) or {}
        self.rules = source.get(self.RULES) or {}
        self.metadata = source.get(self.METADATA) or {}
        self.conditions = source.get(self.CONDITIONS) or {}
        self.resources_with_count = []
        self.tf_parameters = {}
        self._check_resources()
        self.uid = uuid.uuid4().hex
        self.data_for_pseudo_param = {}
        self.tf_region_parameter = None
        self.resources_from_properties: List[ResourcesInProperty] = []

    def _check_resources(self):
        for name, value in self.resources.items():
            if value.get("Condition") or value.get("Count"):
                self.resources_with_count.append(name)

    def get_tf_params(self, param):
        if param in self.tf_parameters:
            return self.tf_parameters[param]
        if param in self.TF_INVALID_VARIABLE_KEYS:
            tf_param = f"{param}_{str(uuid.uuid4())[:8]}"
            self.tf_parameters[param] = tf_param
            return tf_param
        return param

    @classmethod
    def initialize(cls, source: dict, validate: bool = True, _: FileFormat = None):
        if validate:
            cls.validate_ros_template(source)
        rule_manager = RuleManager.initialize(RuleClassifier.ROS)
        return cls(source=source, rule_manager=rule_manager)

    @staticmethod
    def validate_ros_template(source):
        try:
            ros_config = open_api_models.Config(credential=CredClient())
        except CredentialException as e:
            typer.secho(
                f"The Credential is not set, please set credential by:\n"
                f"1. Environment variables (ALIBABA_CLOUD_ACCESS_KEY_ID and ALIBABA_CLOUD_ACCESS_KEY_SECRET)\n"
                f"2. The ini configuration file defined by the environment variable ALIBABA_CLOUD_CREDENTIALS_FILE\n"
                f"3. Alibaba Cloud SDK Credentials default configuration file where"
                f" located in ~/.alibabacloud/credentials.ini",
                fg="red",
            )
            raise InvalidTemplate(reason=f'{e}')
        ros_client = Client(ros_config)
        request = models.ValidateTemplateRequest(template_body=json.dumps(source))
        try:
            runtime = RuntimeOptions(autoretry=True, max_attempts=3, read_timeout=60000, connect_timeout=60000)
            ros_client.validate_template_with_options(request, runtime=runtime)
        except TeaException as e:
            typer.secho(
                f"ROS template is invalid, please check the template and try again, {e}",
                fg="red",
            )
            raise InvalidTemplate(reason=f'{e}')

    def _transform_for_pseudo_param(self, tf_vars, tf_resources):
        if not tf_resources:
            tf_resources = []
        if not tf_vars:
            tf_vars = []
        if self.data_for_pseudo_param:
            tf_resources[:0] = [item for item in self.data_for_pseudo_param.values()]

        if self.tf_region_parameter:
            tf_vars.append(self.tf_region_parameter)

    def transform(self, target_path: str = None, single_file: bool = False):
        """
        transform ros to Terraform
        """
        if not target_path:
            target_path = os.getcwd() + "/terraform/alicloud"
        output_dir = Path(target_path)
        if not output_dir.exists():
            output_dir.mkdir(parents=True, exist_ok=True)
        if not output_dir.is_dir():
            typer.secho(
                f"{target_path} is not a directory",
                fg="red",
            )
            raise InvalidTargetPath(target_path=target_path, reason=f"{target_path} is not a directory")

        typer.secho(f"Transforming ROS template to terraform ...")
        conditions = self._transform_conditions()
        parameters = self._transform_parameters()
        resources = self._transform_resources()
        res_in_properties = self._transform_resource_in_properties()
        resources = resources + res_in_properties
        outputs = self._transform_outputs()
        self._transform_for_pseudo_param(parameters, resources)
        if single_file:
            file_path = (output_dir / "main.tf").resolve()
            with file_path.open("w", encoding="utf-8") as f:
                for block in conditions + parameters + resources + outputs:
                    contents = block.render()
                    f.write(contents)
                    f.write("\n\n")
            tf_files = [file_path]
        else:
            tf_blocks = (('local', conditions), ('variable', parameters), ('main', resources), ('output', outputs))
            tf_files = []
            for block_name, blocks in tf_blocks:
                if not blocks:
                    continue
                file_name = f"{block_name}.tf"
                file_path = (output_dir / file_name).resolve()
                with file_path.open("w", encoding="utf-8") as f:
                    for block in blocks:
                        contents = block.render()
                        f.write(contents)
                        f.write("\n\n")
                tf_files.append(file_path)

        typer.secho(f"Transform successful!\n")

        try:
            tf_command = TerraformCommand(os.path.abspath(output_dir))
            tf_command.fmt(check=True, no_color=False, write=True)
            if not single_file:
                for file_path in tf_files:
                    with file_path.open("r", encoding="utf-8") as f:
                        typer.secho(f.read(), fg="green")
        except TerraformCommandError:
            if not single_file:
                typer.secho(f"Terraform fmt failed, the original content will be displayed.\n", fg="yellow")
                for item in conditions + parameters + resources + outputs:
                    typer.secho(item, fg="green")

    def get_resource_rule(self, ros_res_type: str) -> Optional[ResourceRule]:
        resource_rule: ResourceRule = self.rule_manager.resource_rules.get(ros_res_type)
        if resource_rule is None:
            typer.secho(
                f"Resource type {ros_res_type!r} is not supported and will be ignored.",
                fg="yellow",
            )
            return None
        return resource_rule

    def resolve_values(self, data: Any, tf_type: bool = True) -> Any:
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if key == 'Ref':
                    return functions.ref(self, value)
                elif key in functions.ALL_FUNCTIONS:
                    return functions.ALL_FUNCTIONS[key](self, self.resolve_values(value, False))
                else:
                    result[key] = self.resolve_values(value, tf_type)
        elif isinstance(data, list):
            result = [self.resolve_values(item, tf_type) for item in data]
        else:
            result = data

        if tf_type:
            return tf.convert_to_tf_type(result)
        return result

    def _transform_parameters(self) -> List[tf.Variable]:
        tf_vars = []
        for name, value in self.parameters.items():
            tf_name = self.get_tf_params(camel_to_snake(name))
            value = value.copy()
            param_type = value.pop("Type", None)
            tf_value = dict()
            if param_type == "String":
                tf_value["type"] = "string"
            elif param_type == "Number":
                tf_value["type"] = "number"
            elif param_type == "Boolean":
                tf_value["type"] = "bool"
            elif param_type == "CommaDelimitedList":
                tf_value["type"] = "list(any)"
            else:
                msg = f"The params type {param_type} is not supported, may be ignored when referenced by a resource."
                tf_value["comment-type"] = tf.CommentType(msg)
                tf_value["type"] = "any"

            param_default = value.pop("Default", None)
            if param_default:
                if param_type in ("String", "Number", "Boolean"):
                    tf_value["default"] = tf.convert_to_tf_type(param_default, param_type.lower())
                elif param_type == "CommaDelimitedList" and isinstance(param_default, str):
                    tf_value["default"] = tf.ListOneLineType(
                        [tf.convert_to_tf_type(n) for n in param_default.split(",")])
                else:
                    tf_value["default"] = self.resolve_values(param_default)

            param_sensitive = value.pop("NoEcho", None)
            if param_sensitive:
                tf_value["sensitive"] = tf.BooleanType(True)

            required = value.pop("Required", None)
            if required:
                tf_value["nullable"] = tf.BooleanType(False)

            if value:
                value = json.dumps(value, indent=2, ensure_ascii=False)
                value = textwrap.indent(value, "  ")
                if "${" in value:
                    value = value.replace("${", "$${")
                tf_value["description"] = f"<<EOT\n{value}\n  EOT"
            var_block = tf.Variable(tf_name, tf_value)
            tf_vars.append(var_block)
        return tf_vars

    def _transform_conditions(self) -> List[tf.Locals]:
        tf_conditions = []
        for name, value in self.conditions.items():
            tf_value = {name: self.resolve_values(value)}
            tf_conditions.append(tf.Locals(tf_value))
        return tf_conditions

    def _handled_value(self, tf_argument, schema_value, tf_arg_name, orig_value):
        handler = schema_value.get("Handler")
        if handler:
            handler_func = getattr(functions, handler, None)
            if callable(handler_func):
                if isinstance(tf_arg_name, list):
                    for n in tf_arg_name:
                        tf_argument[n] = handler_func(self, self.resolve_values(orig_value))
                else:
                    tf_argument[tf_arg_name] = handler_func(self, self.resolve_values(orig_value))
                return True

    def _get_tf_argument(
        self,
        res_type: str,
        values: Any,
        schema: dict,
        prop_name: str = None,
        res_name: str = None
    ):
        if isinstance(values, dict):
            tf_argument = {}
            if not schema or not isinstance(schema, dict):
                return self.resolve_values(values)
            for name, value in values.items():
                if prop_name:
                    prop_name = f"{prop_name}.{name}"
                prop_flag = prop_name or name
                msg = f"Resource property {prop_flag!r} of {res_type!r} is not supported and will be ignored."
                if name not in schema:
                    typer.secho(msg, fg="yellow")
                    continue
                schema_value = schema[name] or {}
                tf_arg_name = schema_value.get("To")
                new_res_schema = schema_value.get("ToResources")
                if new_res_schema:
                    new_res_rule = ResourceRule.initialize_from_info(
                        'builtIn',
                        new_res_schema,
                        ResourceRule.SUPPORTED_VERSIONS[0],
                        ResourceRule.RESOURCE
                    )
                    new_prop = value if isinstance(value, list) else [value]
                    new_resources = {}
                    for i, p in enumerate(new_prop):
                        if not isinstance(p, dict):
                            continue
                        new_res_name = f"{res_name}_{name}_{i}_{uuid.uuid4().hex[:8]}"
                        new_resources[new_res_name] = {"Type": f"{res_type}.{name}", "Properties": p}
                        res_in_property = ResourcesInProperty(
                            resource_rule=new_res_rule,
                            resources=new_resources,
                            from_res_name=res_name
                        )
                        self.resources_from_properties.append(res_in_property)
                    continue

                if schema_value.get("Ignore") or not tf_arg_name:
                    typer.secho(msg, fg="yellow")
                    continue

                handled_value = self._handled_value(tf_argument, schema_value, tf_arg_name, value)
                if handled_value:
                    continue

                sub_schema = schema_value.get("Schema")
                sub_args = self._get_tf_argument(res_type, value, sub_schema, name, res_name)
                if sub_schema:
                    if isinstance(sub_args, list):
                        for i, item in enumerate(sub_args):
                            block = tf.Block(tf_arg_name, arguments=item)
                            tf_argument[f"{prop_flag}${i}"] = block
                    elif isinstance(sub_args, dict):
                        block = tf.Block(tf_arg_name, arguments=sub_args)
                        tf_argument[f"{prop_flag}$Block"] = block
                    else:
                        msg = f"The value {sub_args} of arguments {tf_arg_name} is not block and will be ignore."
                        tf_argument[f"{prop_flag}$Comment"] = tf.CommentType(msg)
                else:
                    if isinstance(tf_arg_name, list):
                        for n in tf_arg_name:
                            tf_argument[n] = sub_args
                    else:
                        tf_argument[tf_arg_name] = sub_args

            for name, schema_value in schema.items():
                if not schema_value.get("Required"):
                    continue
                tf_arg_name = schema_value.get("To")
                if not tf_arg_name or tf_arg_name in tf_argument:
                    continue
                self._handled_value(tf_argument, schema_value, tf_arg_name, values.get(name))

            return tf_argument
        elif isinstance(values, list):
            if len(values) == 1 and not schema:
                return self._get_tf_argument(res_type, values[0], schema, prop_name, res_name)
            return tf.JsonType([self._get_tf_argument(res_type, item, schema, prop_name, res_name) for item in values])
        else:
            return self.resolve_values(values)

    def _transform_resources(self, resources_in_property: ResourcesInProperty = None) -> List[tf.Resource]:
        tf_resources = []
        resources = resources_in_property.resources if resources_in_property else self.resources
        for name, res in resources.items():
            tf_name = camel_to_snake(name)
            ros_res_type = res.get("Type")
            if resources_in_property:
                resource_rule = resources_in_property.resource_rule
            else:
                resource_rule = self.get_resource_rule(ros_res_type)
            if not resource_rule:
                typer.secho(
                    f"Resource type {ros_res_type!r} is not supported and will be ignored.",
                    fg="yellow",
                )
                continue

            properties = res.get("Properties")
            resolved_props = self.resolve_values(properties, False) or {}
            tf_argument = self._get_tf_argument(ros_res_type, resolved_props, resource_rule.properties, res_name=name)
            if isinstance(tf_argument, tf.JsonType):
                tf_argument = tf_argument.value

            tf_res_type = resource_rule.target_resource_type
            tf_res_items = []
            built_in_properties = resource_rule.built_in_properties or {}

            def get_built_in_prop_value(built_in_value_schema):
                value_type = built_in_value_schema.get("ValueType")
                ret = built_in_value_schema.get("Value")
                if value_type and value_type == "ResolveAttribute":
                    t_type, expr = ret.split(".", 1)
                    if resources_in_property:
                        n = camel_to_snake(resources_in_property.from_res_name)
                    else:
                        n = f"{tf_name}_{t_type}"
                    return tf.LiteralType(f"{t_type}.{n}.{expr}")
                return tf.QuotedString(ret)

            if ',' in tf_res_type:
                built_in_res_args = defaultdict(dict)
                for prop_name, value_schema in built_in_properties.items():
                    if '.' not in prop_name:
                        continue
                    res_type_key, prop_name = prop_name.split(".")
                    built_in_res_args[res_type_key].update({prop_name: get_built_in_prop_value(value_schema)})

                for tf_type in tf_res_type.split(","):
                    tf_args = {}
                    for key, value in tf_argument.items():
                        if '.' not in key or not key.startswith(tf_type):
                            continue
                        tf_args[key[(len(tf_type) + 1):]] = value
                    built_in_args = built_in_res_args.get(tf_type)
                    if built_in_args:
                        tf_args.update(built_in_args)
                    tf_res_items.append((f"{tf_name}_{tf_type}", tf_type, tf_args))
            else:
                for prop_name, value_schema in built_in_properties.items():
                    tf_argument[prop_name] = get_built_in_prop_value(value_schema)
                tf_res_items.append((tf_name, tf_res_type, tf_argument))

            for tf_name, tf_type, tf_args in tf_res_items:
                condition = res.get("Condition")
                count = res.get("Count")
                if count:
                    tf_args["count"] = self.resolve_values(count)
                if condition:
                    tf_count = tf_args.get("count") if tf_args.get("count") is not None else 1
                    tf_args["count"] = tf.LiteralType(f"local.{condition} ? {tf_count} : 0")

                depends_on = res.get("DependsOn")
                if depends_on:
                    if isinstance(depends_on, str):
                        depends_on = [depends_on]
                    tf_depends_on = []
                    for depend in depends_on:
                        depend_res_type = self.resources[depend]['Type']
                        depend_resource_rule = self.get_resource_rule(depend_res_type)
                        if not depend_resource_rule:
                            typer.secho(
                                f"Resource type {depend_res_type!r} is not supported and will be ignored.",
                                fg="yellow",
                            )
                            continue
                        tf_depend = f"{depend_resource_rule.target_resource_type}.{camel_to_snake(depend)}"
                        tf_depends_on.append(tf.LiteralType(tf_depend))
                    tf_args["depends_on"] = tf.JsonType(tf_depends_on)

                resource = tf.Resource(tf_name, tf_type, tf_args)
                tf_resources.append(resource)
        return tf_resources

    def _transform_resource_in_properties(self) -> List[tf.Resource]:
        if not self.resources_from_properties:
            return []
        result = []
        for resource_in_property in self.resources_from_properties:
            ret = self._transform_resources(resource_in_property)
            result.extend(ret)
        return result


    def _transform_outputs(self) -> List[tf.Output]:
        tf_outputs = []
        for name, value in self.outputs.items():
            tf_name = camel_to_snake(name)
            tf_value = dict()

            resolved_value = self.resolve_values(value.get("Value"))
            condition = value.get("Condition")
            if condition:
                cont_value = f"local.{condition} ? {resolved_value} : {tf.NullType()}"
                tf_value["value"] = tf.LiteralType(cont_value)
            else:
                if isinstance(resolved_value, tf.CommentType):
                    tf_value["value_comment"] = resolved_value
                    tf_value["value"] = tf.NullType()
                else:
                    tf_value["value"] = resolved_value
            desc = value.get("Description")
            if desc:
                if isinstance(desc, str) and "\n" not in desc:
                    tf_value["description"] = tf.QuotedString(desc)
                else:
                    desc = json.dumps(desc, indent=2, ensure_ascii=False)
                    desc = textwrap.indent(desc, "  ")
                    tf_value["description"] = f"<<EOT\n{desc}\n  EOT"

            output_block = tf.Output(tf_name, tf_value)
            tf_outputs.append(output_block)
        return tf_outputs
