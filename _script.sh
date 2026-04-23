#!/bin/bash
cd ~/Desarrollo/projects/sao-platform/terraform
terraform plan -no-color 2>&1 | grep -E "^\s*(#|-).*" | grep -v "^$"
