#!/bin/bash
# Install Hermes skills and gateway service on Photon VM
# Run this from the repo root: bash hermes-skills/install.sh

set -e

SKILLS_DIR="/root/.hermes/skills"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Installing Hermes VMware Skills ==="

# Create skills directory if it doesn't exist
mkdir -p "$SKILLS_DIR"

# Copy skill files
cp hermes-skills/vcenter.py "$SKILLS_DIR/"
cp hermes-skills/vcf_ops.py "$SKILLS_DIR/"
cp hermes-skills/vcf_networks.py "$SKILLS_DIR/"

echo "✓ Skills installed to $SKILLS_DIR:"
ls -la "$SKILLS_DIR"/*.py

echo ""
echo "=== Installing Hermes Gateway Service ==="

# Install systemd service
cp hermes-skills/hermes-gateway.service "$SYSTEMD_DIR/"
systemctl daemon-reload
systemctl enable hermes-gateway.service
systemctl start hermes-gateway.service

echo "✓ hermes-gateway.service enabled and started"
systemctl status hermes-gateway.service --no-pager

echo ""
echo "=== Done! ==="
echo ""
echo "Skills available in Hermes CLI:"
echo "  - vcenter: Query vCenter (VMs, hosts, datastores, alarms, snapshots)"
echo "  - vcf_ops: Query VCF Operations (alerts, health, recommendations)"
echo "  - vcf_networks: Query VCF Networks (topology, NSX segments, flows)"
echo ""
echo "Test with:  /opt/hermes-agent/venv/bin/hermes"
echo "Then ask:   'Use the vcenter skill to list running VMs'"
