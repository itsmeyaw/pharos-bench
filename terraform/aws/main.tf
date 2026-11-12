provider "aws" {
  region = var.region
}

data "http" "my_ip" {
  count = var.ssh_cidr == null ? 1 : 0
  url   = "https://checkip.amazonaws.com"
}

data "aws_ssm_parameter" "ami" {
  name = var.ami_ssm_parameter
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "az" {
  count             = var.availability_zone == null ? 0 : 1
  vpc_id            = data.aws_vpc.default.id
  availability_zone = var.availability_zone
  default_for_az    = true
}

locals {
  ssh_cidr = var.ssh_cidr != null ? var.ssh_cidr : "${chomp(data.http.my_ip[0].response_body)}/32"
  tags     = { Project = "pharos-bench", TTLHours = tostring(var.ttl_hours) }
}

resource "aws_key_pair" "bench" {
  key_name_prefix = "pharos-bench-"
  public_key      = file(pathexpand(var.public_key_path))
  tags            = local.tags
}

resource "aws_security_group" "bench" {
  name_prefix = "pharos-bench-"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.tags

  ingress {
    description = "SSH from the operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [local.ssh_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "bench" {
  ami                                  = data.aws_ssm_parameter.ami.value
  instance_type                        = var.instance_type
  key_name                             = aws_key_pair.bench.key_name
  vpc_security_group_ids               = [aws_security_group.bench.id]
  subnet_id                            = var.availability_zone == null ? null : data.aws_subnet.az[0].id
  instance_initiated_shutdown_behavior = "terminate"
  ebs_optimized                        = true
  user_data                            = "#!/bin/bash\nshutdown -h +${var.ttl_hours * 60}\n"
  tags                                 = merge(local.tags, { Name = "pharos-bench" })

  dynamic "cpu_options" {
    for_each = var.nested_virtualization ? [1] : []
    content {
      nested_virtualization = "enabled"
    }
  }

  root_block_device {
    volume_size = var.volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required"
  }
}
