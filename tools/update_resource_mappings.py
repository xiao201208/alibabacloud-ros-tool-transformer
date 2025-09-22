#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
from collections import OrderedDict


def read_mappings(file_path):
    """
    读取 tf_ali_ros_generate_mappings.json 文件中的映射信息
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        mappings = json.load(f)
    # 按照Terraform资源名称排序
    return OrderedDict(sorted(mappings.items()))


def update_tf_document(mappings, file_path):
    """
    更新 supported-types-tf.md 文件
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 找到表格开始和结束位置
    table_start_pattern = r'针对 Terraform 资源的转换，支持如下：'
    table_end_pattern = r'\| Terraform\s+\| ROS\s+\|'
    
    start_match = re.search(table_start_pattern, content)
    end_match = re.search(table_end_pattern, content)
    
    if not start_match or not end_match:
        raise Exception("无法找到表格位置")
    
    # 表格标题部分
    header_content = content[:end_match.end()]
    
    # 表格内容部分
    table_content = "| Terraform | ROS |\n| --- | --- |\n"
    
    # 生成表格行
    for tf_type, ros_type in mappings.items():
        tf_link = f"[{tf_type}](https://registry.terraform.io/providers/aliyun/alicloud/latest/docs/resources/{tf_type.split('_')[1:]})"
        ros_link = f"[{ros_type}](https://www.alibabacloud.com/help/ros/developer-reference/{ros_type.lower().replace('::', '-').replace('-', '')})"
        table_content += f"| {tf_link} | {ros_link} |\n"
    
    # 保留文件末尾内容（如果有的话）
    end_pos = content.rfind("|")
    if end_pos != -1:
        remaining_content = content[end_pos:]
        # 检查剩余内容是否是表格的一部分
        if not remaining_content.strip().startswith("|"):
            remaining_content = ""
    else:
        remaining_content = ""
    
    # 组合新内容
    new_content = header_content + "\n" + table_content + remaining_content
    
    # 写入文件
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)


def update_ros2tf_document(mappings, file_path):
    """
    更新 supported-types-ros2tf.md 文件
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 找到表格开始和结束位置
    table_start_pattern = r'针对 ROS 模板转 Terraform 模板场景，资源的支持情况如下：'
    table_end_pattern = r'\| ROS\s+\| Terraform\s+\|'
    
    start_match = re.search(table_start_pattern, content)
    end_match = re.search(table_end_pattern, content)
    
    if not start_match or not end_match:
        raise Exception("无法找到表格位置")
    
    # 表格标题部分
    header_content = content[:end_match.end()]
    
    # 表格内容部分
    table_content = "| ROS | Terraform |\n| --- | --- |\n"
    
    # 生成表格行，需要将映射关系反过来
    reversed_mappings = {v: k for k, v in mappings.items()}
    for ros_type, tf_type in sorted(reversed_mappings.items()):
        ros_link = f"[{ros_type}](https://www.alibabacloud.com/help/ros/developer-reference/{ros_type.lower().replace('::', '-').replace('-', '')})"
        tf_link = f"[{tf_type}](https://registry.terraform.io/providers/aliyun/alicloud/latest/docs/resources/{tf_type.split('_')[1:]})"
        table_content += f"| {ros_link} | {tf_link} |\n"
    
    # 保留文件末尾内容（如果有的话）
    end_pos = content.rfind("|")
    if end_pos != -1:
        remaining_content = content[end_pos:]
        # 检查剩余内容是否是表格的一部分
        if not remaining_content.strip().startswith("|"):
            remaining_content = ""
    else:
        remaining_content = ""
    
    # 组合新内容
    new_content = header_content + "\n" + table_content + remaining_content
    
    # 写入文件
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)


def main():
    """
    主函数
    """
    # 文件路径
    mappings_file = 'tools/tf_ali_ros_generate_mappings.json'
    tf_doc_file = 'docs/zh-cn/supported-types-tf.md'
    ros2tf_doc_file = 'docs/zh-cn/supported-types-ros2tf.md'
    
    # 读取映射信息
    print("正在读取映射信息...")
    mappings = read_mappings(mappings_file)
    print(f"共读取 {len(mappings)} 个映射关系")
    
    # 更新 supported-types-tf.md
    print("正在更新 supported-types-tf.md...")
    update_tf_document(mappings, tf_doc_file)
    print("supported-types-tf.md 更新完成")
    
    # 更新 supported-types-ros2tf.md
    print("正在更新 supported-types-ros2tf.md...")
    update_ros2tf_document(mappings, ros2tf_doc_file)
    print("supported-types-ros2tf.md 更新完成")
    
    print("所有文件更新完成！")


if __name__ == "__main__":
    main()