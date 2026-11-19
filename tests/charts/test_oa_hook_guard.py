"""The guard-without-pin securityContext contract over every openshift-ai
hook Job."""

import support

from oa_support import ChartFixture


class HookGuardTests(ChartFixture):
    """Every hook Job container carries the guard-without-pin
    securityContext: the four restricted-pod guards and no
    runAsUser/runAsGroup anywhere in the pod — restricted SCCs assign the
    namespace range UID to UID-less pods, and a pin would fight that
    platform assignment."""

    def all_hook_values(self):
        values = self.managed_maas()
        values["maasDb"] = {"create": True}
        values["maasTenantConfig"] = {
            "enabled": True, "namespace": "models-as-a-service",
            "waitTimeoutSeconds": 240, "pollIntervalSeconds": 5,
            "spec": {"telemetry": {"metrics": {"captureUser": False}}}}
        values["replicas"] = {"dashboard": 1, "operator": None,
                              "asCronJob": True,
                              "schedule": "*/15 * * * *",
                              "waitTimeoutSeconds": 240}
        values["evalhub"] = {"create": True, "tenant": {
            "create": True, "namespace": "evalhub-tenant",
            "waitTimeoutSeconds": 240}}
        return values

    def test_every_hook_job_is_guarded_without_pin(self):
        jobs = support.hook_jobs(self.render(self.all_hook_values()))
        names = [j["metadata"]["name"] for j in jobs]
        # The per-family counts lock the assertion to the full hook-Job
        # inventory; the replica and tenant surface names carry release/
        # namespace hashes, so families are matched by fragment.
        self.assertEqual(len(jobs), 11, names)
        for fragment, count in (("replicas", 2), ("maas-tenant-config", 1),
                                ("create-maas-db-config", 1),
                                ("update-secret-minio", 1),
                                ("dashboard-config", 2), ("mlflow-", 2),
                                ("operator-configs", 2)):
            self.assertEqual(
                sum(1 for n in names if fragment in n), count, names)
        for job in jobs:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])
