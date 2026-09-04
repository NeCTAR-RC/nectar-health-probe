#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_config import cfg
from oslo_messaging import conffixture
import testtools


class TestCase(testtools.TestCase):
    def make_conf(self, transport_url=None):
        """Return a fresh ConfigOpts with messaging opts registered."""
        conf = cfg.ConfigOpts()
        messaging_conf = self.useFixture(conffixture.ConfFixture(conf))
        if transport_url:
            messaging_conf.transport_url = transport_url
        return conf
